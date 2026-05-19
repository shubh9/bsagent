from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import remote_environments
import tools


def _tool_call(name: str, call_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "call_id": call_id,
        "arguments": json.dumps(arguments),
    }


def test_start_environment_builds_droplet_and_registry(
    monkeypatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []
    remote_scripts: list[str] = []

    monkeypatch.setattr(remote_environments, "REGISTRY_DIR", tmp_path)
    monkeypatch.setenv("DIGITALOCEAN_ACCESS_TOKEN", "token")
    monkeypatch.setenv("DO_SSH_KEY_ID", "ssh-key")
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")

    def fake_run_local(command: list[str], *, timeout: int) -> str:
        commands.append(command)
        return "12345 203.0.113.10\n"

    def fake_wait_for_ssh(ip: str) -> None:
        assert ip == "203.0.113.10"

    def fake_run_remote_script(ip: str, script: str, *, timeout: int) -> str:
        assert ip == "203.0.113.10"
        remote_scripts.append(script)
        return ""

    monkeypatch.setattr(remote_environments, "_run_local", fake_run_local)
    monkeypatch.setattr(remote_environments, "_wait_for_ssh", fake_wait_for_ssh)
    monkeypatch.setattr(remote_environments, "_run_remote_script", fake_run_remote_script)

    output = remote_environments.start_environment(size="s-2vcpu-4gb", ttl_minutes=30)
    payload = json.loads(output)

    assert payload["status"] == "ready"
    assert payload["droplet_id"] == "12345"
    assert payload["ip"] == "203.0.113.10"
    assert commands[0][:4] == ["doctl", "compute", "droplet", "create"]
    assert "_" not in commands[0][4]
    assert commands[0][4].startswith("bsagent-renv-")
    assert "--size" in commands[0]
    assert "s-2vcpu-4gb" in commands[0]
    assert "--region" in commands[0]
    assert "nyc3" in commands[0]
    assert "export GITHUB_TOKEN=github-token" in remote_scripts[0]

    registry_path = tmp_path / f"{payload['environment_id']}.json"
    registry = json.loads(registry_path.read_text())
    assert registry["status"] == "ready"
    assert registry["ttl_minutes"] == 30


def test_run_remote_command_records_command_id_and_returns_payload(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(remote_environments, "REGISTRY_DIR", tmp_path)
    metadata = {
        "id": "renv_test",
        "status": "ready",
        "ip": "203.0.113.10",
        "droplet_id": "12345",
        "command_ids": [],
        "created_at": "2026-05-19T00:00:00+00:00",
        "ttl_minutes": 60,
    }
    remote_environments._write_registry(metadata)

    scripts: list[str] = []

    def fake_run_remote_script(ip: str, script: str, *, timeout: int) -> str:
        assert ip == "203.0.113.10"
        scripts.append(script)
        return json.dumps(
            {
                "command_id": "rcmd_test",
                "running": False,
                "exit_code": 0,
                "output": "hello\n",
            }
        )

    monkeypatch.setattr(remote_environments, "_new_id", lambda prefix: f"{prefix}_test")
    monkeypatch.setattr(remote_environments, "_run_remote_script", fake_run_remote_script)

    output = remote_environments.run_remote_command(
        environment_id="renv_test",
        command="echo hello",
        workdir="/workspace",
        timeout=3,
    )
    payload = json.loads(output)

    assert payload["command_id"] == "rcmd_test"
    assert payload["exit_code"] == 0
    assert "command_b64=" in scripts[0]
    registry = json.loads((tmp_path / "renv_test.json").read_text())
    assert registry["command_ids"] == ["rcmd_test"]


def test_remote_tool_schemas_are_registered() -> None:
    tool_names = {tool["name"] for tool in tools.ALL_TOOLS}

    assert "start_remote_environment" in tool_names
    assert "remote_shell_command" in tool_names
    assert "check_remote_command" in tool_names
    assert "stop_remote_environment" in tool_names
    assert tools.tool_supports_parallel("start_remote_environment") is False
    assert tools.tool_supports_parallel("remote_shell_command") is False


def test_remote_tools_route_through_dispatch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tools, "_tool_lock", tools.FairRWLock())

    monkeypatch.setattr(
        tools.remote_environments,
        "start_environment",
        lambda **kwargs: json.dumps({"environment_id": "renv_1", **kwargs}),
    )
    monkeypatch.setattr(
        tools.remote_environments,
        "run_remote_command",
        lambda **kwargs: json.dumps({"command_id": "rcmd_1", **kwargs}),
    )
    monkeypatch.setattr(
        tools.remote_environments,
        "check_remote_command",
        lambda **kwargs: json.dumps({"running": False, **kwargs}),
    )
    monkeypatch.setattr(
        tools.remote_environments,
        "stop_environment",
        lambda **kwargs: json.dumps({"status": "stopped", **kwargs}),
    )

    results = asyncio.run(
        tools.dispatch_tools(
            [
                _tool_call("start_remote_environment", "call-1", {"size": "s-1vcpu-1gb"}),
                _tool_call(
                    "remote_shell_command",
                    "call-2",
                    {
                        "environment_id": "renv_1",
                        "command": "echo hi",
                        "timeout": 1,
                    },
                ),
                _tool_call(
                    "check_remote_command",
                    "call-3",
                    {"environment_id": "renv_1", "command_id": "rcmd_1"},
                ),
                _tool_call(
                    "stop_remote_environment",
                    "call-4",
                    {"environment_id": "renv_1"},
                ),
            ],
            tmp_path,
        )
    )

    assert [result["call_id"] for result in results] == [
        "call-1",
        "call-2",
        "call-3",
        "call-4",
    ]
    assert json.loads(results[0]["output"])["environment_id"] == "renv_1"
    assert json.loads(results[1]["output"])["command"] == "echo hi"
    assert json.loads(results[2]["output"])["command_id"] == "rcmd_1"
    assert json.loads(results[3]["output"])["status"] == "stopped"


def test_remote_tool_errors_are_recoverable(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tools, "_tool_lock", tools.FairRWLock())

    results = asyncio.run(
        tools.dispatch_tools(
            [
                _tool_call(
                    "remote_shell_command",
                    "call-1",
                    {"environment_id": "renv_1"},
                )
            ],
            tmp_path,
        )
    )

    assert "requires a non-empty 'command'" in results[0]["output"]
