"""
DigitalOcean-backed remote shell environments.

The local agent stays in control. These helpers provision an Ubuntu Droplet,
run commands on it over SSH, keep a small local registry, and destroy the
Droplet when asked.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tool_progress import LongRunningProgress, TOOL_ETA_SECONDS

REGISTRY_DIR = Path.home() / ".bsagent" / "remote_environments"
REMOTE_COMMAND_DIR = "/tmp/bsagent-remote-commands"
DEFAULT_REGION = "nyc3"
DEFAULT_IMAGE = "ubuntu-24-04-x64"
DEFAULT_SIZE = "s-1vcpu-1gb"
DEFAULT_TTL_MINUTES = 60
SSH_USER = "root"
OUTPUT_CAP = 8_000


class RemoteEnvironmentError(Exception):
    """A recoverable remote-environment operation failure."""


def start_environment(
    *,
    size: str = DEFAULT_SIZE,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> str:
    _require_env("DIGITALOCEAN_ACCESS_TOKEN")
    ssh_key_id = _require_env("DO_SSH_KEY_ID")

    env_id = _new_id("renv")
    # DigitalOcean Droplet names must be hostname-safe; keep underscores only
    # in our local environment_id.
    name = f"bsagent-{env_id.replace('_', '-')}"
    region = os.environ.get("DO_REGION", DEFAULT_REGION)
    image = os.environ.get("DO_IMAGE", DEFAULT_IMAGE)

    metadata: dict[str, Any] = {
        "id": env_id,
        "name": name,
        "provider": "digitalocean",
        "droplet_id": "",
        "ip": "",
        "ssh_user": SSH_USER,
        "region": region,
        "image": image,
        "size": size,
        "ttl_minutes": int(ttl_minutes),
        "created_at": _now_iso(),
        "status": "provisioning",
        "command_ids": [],
    }

    try:
        with LongRunningProgress(
            "start_remote_environment",
            eta_seconds=TOOL_ETA_SECONDS["start_remote_environment"],
            phase="creating droplet",
        ) as progress:
            create = _run_local(
                [
                    "doctl",
                    "compute",
                    "droplet",
                    "create",
                    name,
                    "--size",
                    size,
                    "--image",
                    image,
                    "--region",
                    region,
                    "--ssh-keys",
                    ssh_key_id,
                    "--tag-names",
                    "bsagent,bsagent-remote-env",
                    "--wait",
                    "--format",
                    "ID,PublicIPv4",
                    "--no-header",
                ],
                timeout=180,
            )
            parts = create.strip().split()
            if len(parts) < 2:
                raise RemoteEnvironmentError(f"could not parse doctl output: {create!r}")

            droplet_id, ip = parts[0], parts[1]
            metadata["droplet_id"] = droplet_id
            metadata["ip"] = ip
            _write_registry(metadata)

            progress.set_phase("waiting for SSH")
            _wait_for_ssh(ip)
            progress.set_phase("bootstrapping VM (cloud-init + packages)")
            _run_remote_script(ip, _setup_script(), timeout=900)
    except BaseException:
        metadata["status"] = "provision_failed"
        _write_registry(metadata)
        raise

    metadata["status"] = "ready"
    _write_registry(metadata)
    return _json(
        {
            "environment_id": env_id,
            "status": "ready",
            "droplet_id": droplet_id,
            "ip": ip,
            "ssh_user": SSH_USER,
            "size": size,
            "ttl_minutes": int(ttl_minutes),
        }
    )


def list_environments() -> str:
    environments = []
    for path in sorted(REGISTRY_DIR.glob("*.json")):
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        environments.append(_with_age_and_expiry(metadata))
    return _json({"environments": environments})


def run_remote_command(
    *,
    environment_id: str,
    command: str,
    workdir: str = "/workspace",
    timeout: int = 60,
) -> str:
    if not command:
        raise RemoteEnvironmentError("remote_shell_command requires a non-empty command")

    metadata = _read_registry(environment_id)
    _ensure_active(metadata)

    command_id = _new_id("rcmd")
    command_b64 = base64.b64encode(command.encode("utf-8")).decode("ascii")
    script = _start_command_script(
        command_id=command_id,
        command_b64=command_b64,
        workdir=workdir,
        wait_seconds=max(0, int(timeout)),
    )
    remote_timeout = max(30, int(timeout) + 30)
    with LongRunningProgress(
        f"remote_shell_command ({command_id})",
        eta_seconds=max(30, int(timeout) + 15),
        phase="running on VM",
    ):
        output = _run_remote_script(metadata["ip"], script, timeout=remote_timeout)
    payload = _parse_json_output(output)

    command_ids = list(metadata.get("command_ids", []))
    if command_id not in command_ids:
        command_ids.append(command_id)
    metadata["command_ids"] = command_ids
    metadata["last_command_id"] = command_id
    _write_registry(metadata)

    return _json(payload)


def check_remote_command(*, environment_id: str, command_id: str) -> str:
    metadata = _read_registry(environment_id)
    _ensure_active(metadata)
    output = _run_remote_script(
        metadata["ip"],
        _check_command_script(command_id=command_id),
        timeout=30,
    )
    return _json(_parse_json_output(output))


def stop_environment(*, environment_id: str) -> str:
    metadata = _read_registry(environment_id)
    droplet_id = metadata.get("droplet_id")
    if not droplet_id:
        raise RemoteEnvironmentError(f"environment {environment_id!r} has no droplet_id")

    with LongRunningProgress(
        "stop_remote_environment",
        eta_seconds=TOOL_ETA_SECONDS["stop_remote_environment"],
        phase="destroying droplet",
    ):
        _run_local(
            ["doctl", "compute", "droplet", "delete", str(droplet_id), "--force"],
            timeout=120,
        )
    metadata["status"] = "stopped"
    metadata["stopped_at"] = _now_iso()
    _write_registry(metadata)
    return _json(
        {
            "environment_id": environment_id,
            "status": "stopped",
            "droplet_id": droplet_id,
        }
    )


def _setup_script() -> str:
    env_lines = _remote_env_exports()
    return f"""\
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "Waiting for cloud-init and apt locks..."
cloud-init status --wait || true
while fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock >/dev/null 2>&1; do
  sleep 3
done

apt-get update -y
apt-get install -y git python3 python3-venv python3-pip build-essential ripgrep tmux curl unzip

mkdir -p /workspace {REMOTE_COMMAND_DIR} /root/.bsagent
chmod 700 /root/.bsagent
cat > /root/.bsagent/env <<'REMOTE_ENV'
{env_lines}
REMOTE_ENV
chmod 600 /root/.bsagent/env
"""


def _remote_env_exports() -> str:
    lines = []
    for key in ("GITHUB_TOKEN", "OPENAI_API_KEY"):
        value = os.environ.get(key)
        if value:
            lines.append(f"export {key}={shlex.quote(value)}")
    return "\n".join(lines)


def _start_command_script(
    *,
    command_id: str,
    command_b64: str,
    workdir: str,
    wait_seconds: int,
) -> str:
    return f"""\
set -euo pipefail

command_id={shlex.quote(command_id)}
command_b64={shlex.quote(command_b64)}
workdir={shlex.quote(workdir)}
wait_seconds={int(wait_seconds)}
command_dir={shlex.quote(REMOTE_COMMAND_DIR)}

mkdir -p "$command_dir" "$workdir"
command_file="$command_dir/$command_id.sh"
log_file="$command_dir/$command_id.log"
status_file="$command_dir/$command_id.status"
pid_file="$command_dir/$command_id.pid"

python3 - "$command_b64" "$command_file" <<'PY'
import base64
import pathlib
import sys

pathlib.Path(sys.argv[2]).write_bytes(base64.b64decode(sys.argv[1]))
PY
chmod 700 "$command_file"
rm -f "$log_file" "$status_file" "$pid_file"

nohup bash -c '
  set +e
  if [[ -f /root/.bsagent/env ]]; then
    set -a
    source /root/.bsagent/env
    set +a
  fi
  cd "$1"
  bash -lc "$(cat "$2")"
  code=$?
  printf "%s" "$code" > "$3.tmp"
  mv "$3.tmp" "$3"
' _ "$workdir" "$command_file" "$status_file" > "$log_file" 2>&1 &

pid=$!
printf "%s" "$pid" > "$pid_file"

deadline=$((SECONDS + wait_seconds))
while [[ ! -f "$status_file" && "$SECONDS" -lt "$deadline" ]]; do
  sleep 1
done

python3 - "$command_id" "$log_file" "$status_file" "$pid_file" <<'PY'
import json
import os
import pathlib
import signal
import sys

command_id, log_path, status_path, pid_path = sys.argv[1:]

def read(path, default=""):
    try:
        return pathlib.Path(path).read_text(errors="replace")
    except FileNotFoundError:
        return default

status_text = read(status_path).strip()
pid_text = read(pid_path).strip()
running = False
if pid_text and not status_text:
    try:
        os.kill(int(pid_text), 0)
        running = True
    except OSError:
        running = False

log = read(log_path)
tail = log[-{OUTPUT_CAP}:]
print(json.dumps({{
    "command_id": command_id,
    "running": running,
    "exit_code": int(status_text) if status_text else None,
    "log_path": log_path,
    "status_path": status_path,
    "pid": int(pid_text) if pid_text else None,
    "output": tail,
    "truncated": len(log) > {OUTPUT_CAP},
}}))
PY
"""


def _check_command_script(*, command_id: str) -> str:
    return f"""\
set -euo pipefail
command_id={shlex.quote(command_id)}
command_dir={shlex.quote(REMOTE_COMMAND_DIR)}
log_file="$command_dir/$command_id.log"
status_file="$command_dir/$command_id.status"
pid_file="$command_dir/$command_id.pid"

python3 - "$command_id" "$log_file" "$status_file" "$pid_file" <<'PY'
import json
import os
import pathlib
import sys

command_id, log_path, status_path, pid_path = sys.argv[1:]

def read(path, default=""):
    try:
        return pathlib.Path(path).read_text(errors="replace")
    except FileNotFoundError:
        return default

status_text = read(status_path).strip()
pid_text = read(pid_path).strip()
running = False
if pid_text and not status_text:
    try:
        os.kill(int(pid_text), 0)
        running = True
    except OSError:
        running = False

log = read(log_path)
tail = log[-{OUTPUT_CAP}:]
print(json.dumps({{
    "command_id": command_id,
    "running": running,
    "exit_code": int(status_text) if status_text else None,
    "log_path": log_path,
    "status_path": status_path,
    "pid": int(pid_text) if pid_text else None,
    "output": tail,
    "truncated": len(log) > {OUTPUT_CAP},
}}))
PY
"""


def _run_local(command: list[str], *, timeout: int) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
    except FileNotFoundError as exc:
        raise RemoteEnvironmentError(
            f"required command not found: {command[0]!r}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RemoteEnvironmentError(
            f"command timed out after {timeout}s: {shlex.join(command)}"
        ) from exc

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise RemoteEnvironmentError(
            f"command failed with exit={result.returncode}: {shlex.join(command)}\n"
            f"{_cap(output)}"
        )
    return output


def _run_remote_script(ip: str, script: str, *, timeout: int) -> str:
    ssh_target = f"{SSH_USER}@{ip}"
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "StrictHostKeyChecking=accept-new",
                ssh_target,
                "bash -s",
            ],
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RemoteEnvironmentError("required command not found: 'ssh'") from exc
    except subprocess.TimeoutExpired as exc:
        raise RemoteEnvironmentError(
            f"remote command timed out after {timeout}s on {ssh_target}"
        ) from exc

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise RemoteEnvironmentError(
            f"remote command failed with exit={result.returncode} on {ssh_target}\n"
            f"{_cap(output)}"
        )
    return output


def _wait_for_ssh(ip: str) -> None:
    deadline = time.monotonic() + 300
    last_error = ""
    while time.monotonic() < deadline:
        try:
            _run_remote_script(ip, "true\n", timeout=15)
            return
        except RemoteEnvironmentError as exc:
            last_error = str(exc)
            time.sleep(5)
    raise RemoteEnvironmentError(f"SSH did not become ready for {ip}: {last_error}")


def _parse_json_output(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise RemoteEnvironmentError(f"remote command did not return JSON:\n{_cap(output)}")


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RemoteEnvironmentError(f"missing required env var: {name}")
    return value


def _registry_path(environment_id: str) -> Path:
    if not environment_id.startswith("renv_"):
        raise RemoteEnvironmentError(f"invalid environment_id: {environment_id!r}")
    return REGISTRY_DIR / f"{environment_id}.json"


def _read_registry(environment_id: str) -> dict[str, Any]:
    path = _registry_path(environment_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RemoteEnvironmentError(f"unknown environment_id: {environment_id}") from exc
    except json.JSONDecodeError as exc:
        raise RemoteEnvironmentError(f"corrupt registry entry: {path}") from exc


def _write_registry(metadata: dict[str, Any]) -> None:
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    path = _registry_path(str(metadata["id"]))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _ensure_active(metadata: dict[str, Any]) -> None:
    if metadata.get("status") == "stopped":
        raise RemoteEnvironmentError(f"environment {metadata.get('id')} is stopped")


def _with_age_and_expiry(metadata: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(metadata)
    created_at = str(metadata.get("created_at", ""))
    try:
        created = datetime.fromisoformat(created_at)
        age_seconds = max(0, int((datetime.now(timezone.utc) - created).total_seconds()))
    except ValueError:
        age_seconds = 0
    ttl_minutes = int(metadata.get("ttl_minutes", DEFAULT_TTL_MINUTES))
    enriched["age_seconds"] = age_seconds
    enriched["ttl_expired"] = age_seconds > ttl_minutes * 60
    return enriched


def _new_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{prefix}_{stamp}_{secrets.token_hex(3)}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cap(text: str) -> str:
    if len(text) > OUTPUT_CAP:
        return text[:OUTPUT_CAP] + f"\n... [truncated, {len(text) - OUTPUT_CAP} more chars]"
    return text


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)
