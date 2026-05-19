from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
AGENT_PY = ROOT / "agent.py"
PYTHON = sys.executable


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _tagged_remote_droplet_ids() -> set[str]:
    result = subprocess.run(
        [
            "doctl",
            "compute",
            "droplet",
            "list",
            "--tag-name",
            "bsagent-remote-env",
            "--format",
            "ID",
            "--no-header",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _delete_droplet(droplet_id: str) -> None:
    subprocess.run(
        ["doctl", "compute", "droplet", "delete", droplet_id, "--force"],
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.e2e
def test_real_agent_prompt_uses_remote_environment_tools(tmp_path: Path) -> None:
    """
    Real paid-infrastructure e2e for the user-facing remote VM flow.

    This intentionally runs the same style of message a user sends to bsagent,
    rather than calling remote environment helpers directly.

    Opt in explicitly:
      BSAGENT_RUN_REAL_REMOTE_E2E=1 python -m pytest tests/test_remote_environments_e2e.py -q -s
    """
    _load_dotenv()
    if os.environ.get("BSAGENT_RUN_REAL_REMOTE_E2E") != "1":
        pytest.skip("set BSAGENT_RUN_REAL_REMOTE_E2E=1 to create a real DigitalOcean VM")

    for name in ("DIGITALOCEAN_ACCESS_TOKEN", "DO_SSH_KEY_ID"):
        if not os.environ.get(name):
            pytest.skip(f"missing required env var: {name}")

    before = _tagged_remote_droplet_ids()
    prompt = (
        "Start a remote environment with size s-1vcpu-1gb and ttl 15 minutes. "
        "In that remote environment, run `pwd; hostname; uname -a; python3 --version; "
        "echo REMOTE_ENV_OK` from /workspace with a 30 second timeout. "
        "Then run a longer remote command `echo LONG_START; sleep 5; echo LONG_DONE` "
        "with a 1 second timeout, poll it with check_remote_command until it finishes, "
        "and finally stop the remote environment. "
        "You must use the tools start_remote_environment, remote_shell_command, "
        "check_remote_command, and stop_remote_environment. "
        "Your final response must include the literal strings REMOTE_ENV_OK, "
        "LONG_START, LONG_DONE, and stopped."
    )
    env = {
        **os.environ,
        "AGENT_WORKDIR": str(tmp_path),
        "AGENT_MODEL": os.environ.get("AGENT_MODEL", "gpt-5.5"),
    }
    try:
        result = subprocess.run(
            [PYTHON, str(AGENT_PY), prompt],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
        )
    finally:
        after = _tagged_remote_droplet_ids()
        for droplet_id in sorted(after - before):
            _delete_droplet(droplet_id)

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    for tool_name in [
        "start_remote_environment",
        "remote_shell_command",
        "check_remote_command",
        "stop_remote_environment",
    ]:
        assert tool_name in combined, combined
    for marker in ["REMOTE_ENV_OK", "LONG_START", "LONG_DONE"]:
        assert marker in combined, combined
    assert "stopped" in combined.lower(), combined
