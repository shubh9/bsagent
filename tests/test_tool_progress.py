from __future__ import annotations

import io
import time

import tool_progress


def test_long_running_progress_updates_elapsed(monkeypatch) -> None:
    stderr = io.StringIO()
    monkeypatch.setattr(tool_progress, "_HAS_RICH", False)
    monkeypatch.setattr(tool_progress.sys, "stderr", stderr)

    with tool_progress.LongRunningProgress(
        "start_remote_environment",
        eta_seconds=120,
        phase="creating droplet",
        tick_interval=0.05,
    ):
        time.sleep(0.12)

    output = stderr.getvalue()
    assert "start_remote_environment" in output
    assert "~120s typical" in output
    assert "finished in" in output


def test_eta_for_remote_shell_command_uses_timeout() -> None:
    eta = tool_progress.eta_for_tool(
        "remote_shell_command",
        {"timeout": 600},
    )
    assert eta == 630
