"""
Lightweight stderr progress for long-running tools.

Shows elapsed time and a rough ETA so provisioning does not look hung.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

try:
    from rich.console import Console
    from rich.status import Status

    _console = Console(stderr=True, highlight=False)
    _HAS_RICH = True
except ImportError:  # pragma: no cover - rich is in requirements.txt
    _console = None
    Status = None  # type: ignore[misc, assignment]
    _HAS_RICH = False

# Rough expected durations for user-facing feedback (seconds).
TOOL_ETA_SECONDS: dict[str, int] = {
    "start_remote_environment": 300,
    "remote_shell_command": 90,
    "check_remote_command": 15,
    "stop_remote_environment": 45,
}


class LongRunningProgress:
    """Show one live status line (Rich) or sparse phase lines (plain fallback)."""

    def __init__(
        self,
        label: str,
        *,
        eta_seconds: int,
        phase: str = "",
        tick_interval: float = 1.0,
    ) -> None:
        self.label = label
        self.eta_seconds = max(1, eta_seconds)
        self.phase = phase
        self.tick_interval = tick_interval
        self._started = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._status: Status | None = None
        self._last_plain_line = ""

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        self._refresh()

    def __enter__(self) -> "LongRunningProgress":
        if _HAS_RICH and _console is not None:
            self._status = _console.status(self._message(), spinner="dots")
            self._status.start()
        else:
            self._print_plain_line()

        self._thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        with self._lock:
            if self._status is not None:
                self._status.stop()
                self._status = None
        elapsed = int(time.monotonic() - self._started)
        if exc_type is None:
            self._print_done(elapsed)
        else:
            self._print_failed(elapsed)

    def _tick_loop(self) -> None:
        while not self._stop.wait(self.tick_interval):
            self._refresh()

    def _elapsed(self) -> int:
        return max(0, int(time.monotonic() - self._started))

    def _message(self) -> str:
        elapsed = self._elapsed()
        phase = f" — {self.phase}" if self.phase else ""
        return (
            f"[dim]⏳ {self.label}{phase}  "
            f"{elapsed}s elapsed · ~{self.eta_seconds}s typical[/dim]"
        )

    def _plain_message(self) -> str:
        elapsed = self._elapsed()
        phase = f" — {self.phase}" if self.phase else ""
        return f"⏳ {self.label}{phase}  {elapsed}s elapsed · ~{self.eta_seconds}s typical"

    def _refresh(self) -> None:
        with self._lock:
            if self._status is not None:
                self._status.update(self._message())
            else:
                self._print_plain_line()

    def _print_plain_line(self) -> None:
        line = self._plain_message()
        if line == self._last_plain_line:
            return
        self._last_plain_line = line
        sys.stderr.write(f"\n\033[2m{line}\033[0m")
        sys.stderr.flush()

    def _print_done(self, elapsed: int) -> None:
        message = f"  ✓ {self.label} finished in {elapsed}s"
        if _HAS_RICH and _console is not None:
            _console.print(f"[green]{message}[/green]")
        else:
            sys.stderr.write(f"\n\033[32m{message}\033[0m\n")
            sys.stderr.flush()

    def _print_failed(self, elapsed: int) -> None:
        message = f"  ✗ {self.label} failed after {elapsed}s"
        if _HAS_RICH and _console is not None:
            _console.print(f"[red]{message}[/red]")
        else:
            sys.stderr.write(f"\n\033[31m{message}\033[0m\n")
            sys.stderr.flush()


def eta_for_tool(tool_name: str, args: dict[str, Any] | None = None) -> int:
    if tool_name == "remote_shell_command" and args:
        return max(30, int(args.get("timeout", 60)) + 30)
    return TOOL_ETA_SECONDS.get(tool_name, 60)
