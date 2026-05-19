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

    _console = Console(stderr=True, highlight=False)
    _HAS_RICH = True
except ImportError:  # pragma: no cover - rich is in requirements.txt
    _console = None
    _HAS_RICH = False

# Rough expected durations for user-facing feedback (seconds).
TOOL_ETA_SECONDS: dict[str, int] = {
    "start_remote_environment": 300,
    "remote_shell_command": 90,
    "check_remote_command": 15,
    "stop_remote_environment": 45,
}


class LongRunningProgress:
    """Print a single updating status line until closed."""

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
        self._last_line = ""

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        self._render(force_newline=False)

    def __enter__(self) -> "LongRunningProgress":
        self._render(force_newline=True)
        self._thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        elapsed = int(time.monotonic() - self._started)
        self._clear_line()
        if exc_type is None:
            self._print_done(elapsed)
        else:
            self._print_failed(elapsed)

    def _tick_loop(self) -> None:
        while not self._stop.wait(self.tick_interval):
            self._render(force_newline=False)

    def _elapsed(self) -> int:
        return max(0, int(time.monotonic() - self._started))

    def _render(self, *, force_newline: bool) -> None:
        elapsed = self._elapsed()
        phase = f" — {self.phase}" if self.phase else ""
        line = (
            f"  ⏳ {self.label}{phase}  "
            f"{elapsed}s elapsed · ~{self.eta_seconds}s typical"
        )
        if _HAS_RICH and _console is not None:
            if force_newline:
                _console.print(f"[dim]{line}[/dim]")
            else:
                self._clear_line()
                _console.print(f"[dim]{line}[/dim]", end="\r")
            self._last_line = line
            return

        if force_newline:
            sys.stderr.write(f"\n\033[2m{line}\033[0m\n")
        else:
            sys.stderr.write(f"\r\033[2m{line}\033[0m")
        sys.stderr.flush()
        self._last_line = line

    def _clear_line(self) -> None:
        if not self._last_line:
            return
        if _HAS_RICH and _console is not None:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()
        else:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()
        self._last_line = ""

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
