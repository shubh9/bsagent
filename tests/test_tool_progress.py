from __future__ import annotations

import io
import time

import tool_progress


def test_long_running_progress_plain_mode_only_prints_on_phase_change(
    monkeypatch,
) -> None:
    stderr = io.StringIO()
    monkeypatch.setattr(tool_progress, "_HAS_RICH", False)
    monkeypatch.setattr(tool_progress.sys, "stderr", stderr)

    with tool_progress.LongRunningProgress(
        "start_remote_environment",
        eta_seconds=120,
        phase="creating droplet",
        tick_interval=0.05,
    ) as progress:
        time.sleep(0.12)
        progress.set_phase("waiting for SSH")

    output = stderr.getvalue()
    assert output.count("⏳") == 2
    assert "creating droplet" in output
    assert "waiting for SSH" in output
    assert "finished in" in output


def test_eta_for_remote_shell_command_uses_timeout() -> None:
    eta = tool_progress.eta_for_tool(
        "remote_shell_command",
        {"timeout": 600},
    )
    assert eta == 630
