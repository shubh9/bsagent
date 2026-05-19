"""
Hacky local billing heartbeat for remote environments.

This is intentionally local-first: every heartbeat rewrites one JSON file with
the latest uptime and estimated cost. A production version can replace
``write_billing`` with an API call.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

BILLING_FILE = Path.home() / ".bsagent" / "remote_billing.json"
HEARTBEAT_INTERVAL_SECONDS = 60
BUDGET_USD: float | None = None

# Per-minute cost config. Defaults are DigitalOcean monthly prices / 730h / 60m.
SIZE_PER_MINUTE_COST_USD: dict[str, float] = {
    "s-1vcpu-1gb": 6.0 / 730 / 60,
    "s-1vcpu-2gb": 12.0 / 730 / 60,
    "s-2vcpu-2gb": 18.0 / 730 / 60,
    "s-2vcpu-4gb": 24.0 / 730 / 60,
    "s-4vcpu-8gb": 48.0 / 730 / 60,
    "s-8vcpu-16gb": 96.0 / 730 / 60,
}

_lock = threading.Lock()
_stop_events: dict[str, threading.Event] = {}
_threads: dict[str, threading.Thread] = {}


def register_environment(metadata: dict[str, Any]) -> None:
    environment_id = str(metadata["id"])
    with _lock:
        data = _read_billing_unlocked()
        environments = data.setdefault("environments", {})
        environments[environment_id] = _record_from_metadata(metadata)
        _recompute_totals(data)
        _write_billing_unlocked(data)


def start_heartbeat(
    *,
    environment_id: str,
    get_metadata: Callable[[], dict[str, Any]],
    is_alive: Callable[[dict[str, Any]], bool],
    stop_environment: Callable[[str, str], None],
) -> None:
    stop_heartbeat(environment_id)
    stop_event = threading.Event()
    _stop_events[environment_id] = stop_event

    def loop() -> None:
        while not stop_event.wait(HEARTBEAT_INTERVAL_SECONDS):
            try:
                heartbeat_once(
                    environment_id=environment_id,
                    get_metadata=get_metadata,
                    is_alive=is_alive,
                    stop_environment=stop_environment,
                )
            except Exception:
                # Billing should never crash the agent loop.
                continue
            if environment_id not in _stop_events:
                break

    thread = threading.Thread(
        target=loop,
        name=f"bsagent-billing-{environment_id}",
        daemon=True,
    )
    _threads[environment_id] = thread
    thread.start()


def heartbeat_once(
    *,
    environment_id: str,
    get_metadata: Callable[[], dict[str, Any]],
    is_alive: Callable[[dict[str, Any]], bool],
    stop_environment: Callable[[str, str], None],
) -> dict[str, Any] | None:
    metadata = get_metadata()
    if metadata.get("status") == "stopped":
        finalize_environment(metadata)
        stop_heartbeat(environment_id)
        return None

    alive = is_alive(metadata)
    with _lock:
        data = _read_billing_unlocked()
        environments = data.setdefault("environments", {})
        record = environments.get(environment_id)
        if not isinstance(record, dict):
            record = _record_from_metadata(metadata)
            environments[environment_id] = record

        _update_record(record, metadata, alive=alive)
        if not alive:
            record["status"] = "unreachable"
            record["stopped_reason"] = "heartbeat_unreachable"
            record["stopped_at"] = _now_iso()

        _recompute_totals(data)
        running_cost = float(data.get("running_estimated_cost_usd", 0.0))
        _write_billing_unlocked(data)
        snapshot = dict(record)

    if not alive:
        stop_heartbeat(environment_id)
        return snapshot

    if BUDGET_USD is not None and running_cost > BUDGET_USD:
        stop_environment(environment_id, "budget_exceeded")
        stop_heartbeat(environment_id)

    return snapshot


def finalize_environment(
    metadata: dict[str, Any],
    *,
    stopped_reason: str | None = None,
) -> None:
    environment_id = str(metadata["id"])
    with _lock:
        data = _read_billing_unlocked()
        environments = data.setdefault("environments", {})
        record = environments.get(environment_id)
        if not isinstance(record, dict):
            record = _record_from_metadata(metadata)
            environments[environment_id] = record

        _update_record(record, metadata, alive=False)
        record["status"] = "stopped"
        record["stopped_at"] = str(metadata.get("stopped_at") or _now_iso())
        record["stopped_reason"] = stopped_reason or record.get("stopped_reason")
        _recompute_totals(data)
        _write_billing_unlocked(data)


def stop_heartbeat(environment_id: str) -> None:
    stop_event = _stop_events.pop(environment_id, None)
    if stop_event is not None:
        stop_event.set()
    _threads.pop(environment_id, None)


def _record_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    size = str(metadata.get("size") or "")
    started_at = str(metadata.get("created_at") or _now_iso())
    return {
        "environment_id": metadata["id"],
        "droplet_id": metadata.get("droplet_id"),
        "size": size,
        "per_minute_cost_usd": _per_minute_cost(size),
        "started_at": started_at,
        "last_heartbeat": started_at,
        "stopped_at": None,
        "status": "running",
        "alive": True,
        "billed_seconds": 0,
        "estimated_cost_usd": 0.0,
        "stopped_reason": None,
    }


def _update_record(
    record: dict[str, Any],
    metadata: dict[str, Any],
    *,
    alive: bool,
) -> None:
    started_at = _parse_iso(str(record.get("started_at") or metadata.get("created_at", "")))
    if started_at is None:
        started_at = datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    billed_seconds = max(0, int((now - started_at).total_seconds()))
    per_minute_cost = float(record.get("per_minute_cost_usd", 0.0))
    record["alive"] = alive
    record["last_heartbeat"] = now.isoformat()
    record["billed_seconds"] = billed_seconds
    record["estimated_cost_usd"] = round((billed_seconds / 60.0) * per_minute_cost, 6)


def _per_minute_cost(size: str) -> float:
    return SIZE_PER_MINUTE_COST_USD.get(size, SIZE_PER_MINUTE_COST_USD["s-1vcpu-1gb"])


def _read_billing_unlocked() -> dict[str, Any]:
    try:
        data = json.loads(BILLING_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("budget_usd", BUDGET_USD)
    data.setdefault("environments", {})
    return data


def _write_billing_unlocked(data: dict[str, Any]) -> None:
    BILLING_FILE.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _now_iso()
    tmp = BILLING_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(BILLING_FILE)


def _recompute_totals(data: dict[str, Any]) -> None:
    total = 0.0
    running = 0.0
    for record in data.get("environments", {}).values():
        if not isinstance(record, dict):
            continue
        cost = float(record.get("estimated_cost_usd", 0.0))
        total += cost
        if record.get("status") == "running":
            running += cost
    data["total_estimated_cost_usd"] = round(total, 6)
    data["running_estimated_cost_usd"] = round(running, 6)


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
