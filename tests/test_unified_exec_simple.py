from __future__ import annotations

import asyncio
import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import tools
from unified_exec import OutputBuffer, PexpectTerminalBackend, process_manager


PYTHON = shlex.quote(sys.executable)


@pytest.fixture(autouse=True)
def cleanup_sessions(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tools, "_tool_lock", tools.FairRWLock())
    monkeypatch.setattr(
        tools,
        "TOOL_PARALLEL_CONFIG",
        {
            "parallel_tools": {"exec_command", "write_stdin"},
            "exclusive_tools": {"apply_patch"},
        },
    )
    yield
    asyncio.run(process_manager.terminate_all())


def _call(name: str, call_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "call_id": call_id,
        "arguments": json.dumps(arguments),
    }


def _exec_call(
    call_id: str,
    command: str,
    *,
    yield_time_ms: int = 1000,
) -> dict[str, Any]:
    return _call(
        "exec_command",
        call_id,
        {"cmd": command, "yield_time_ms": yield_time_ms},
    )


def _write_call(
    call_id: str,
    session_id: int,
    chars: str,
    *,
    yield_time_ms: int = 1000,
) -> dict[str, Any]:
    return _call(
        "write_stdin",
        call_id,
        {
            "session_id": session_id,
            "chars": chars,
            "yield_time_ms": yield_time_ms,
        },
    )


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    return json.loads(result["output"])


def test_output_buffer_caps_and_reports_truncation() -> None:
    buffer = OutputBuffer(max_chars=10)

    buffer.append("hello")
    buffer.append(" world")
    snapshot = buffer.snapshot_all(100)

    assert "ello world" in snapshot.output
    assert snapshot.original_char_count == 11
    assert snapshot.truncated


def test_pexpect_backend_runs_short_command(tmp_path: Path) -> None:
    session = PexpectTerminalBackend().spawn("echo hello", tmp_path)

    read = session.read_until_idle_or_exit(2)

    assert "hello" in read.output
    assert read.exit_code == 0
    assert not read.alive


def test_parallel_pty_commands_are_faster_than_sequential(tmp_path: Path) -> None:
    calls = [
        _exec_call(
            "call-1",
            f"{PYTHON} -c \"import time; time.sleep(1); print('one')\"",
            yield_time_ms=2500,
        ),
        _exec_call(
            "call-2",
            f"{PYTHON} -c \"import time; time.sleep(1); print('two')\"",
            yield_time_ms=2500,
        ),
        _exec_call(
            "call-3",
            f"{PYTHON} -c \"import time; time.sleep(1); print('three')\"",
            yield_time_ms=2500,
        ),
    ]

    started = time.perf_counter()
    results = asyncio.run(tools.dispatch_tools(calls, tmp_path))
    elapsed = time.perf_counter() - started
    payloads = [_payload(result) for result in results]

    assert elapsed < 2
    assert all(payload["exit_code"] == 0 for payload in payloads)
    assert "one" in payloads[0]["output"]
    assert "two" in payloads[1]["output"]
    assert "three" in payloads[2]["output"]


def test_fast_exec_command_does_not_return_session_id(tmp_path: Path) -> None:
    results = asyncio.run(
        tools.dispatch_tools([_exec_call("call-1", "echo hello")], tmp_path)
    )

    payload = _payload(results[0])
    assert payload["exit_code"] == 0
    assert "hello" in payload["output"]
    assert "session_id" not in payload


def test_long_command_returns_session_id_and_can_be_polled(tmp_path: Path) -> None:
    async def scenario() -> None:
        command = (
            f"{PYTHON} -u -c \"import time; print('start', flush=True); "
            "time.sleep(1); print('done', flush=True)\""
        )
        start_results = await tools.dispatch_tools(
            [_exec_call("call-1", command, yield_time_ms=100)], tmp_path
        )

        start_payload = _payload(start_results[0])
        assert start_payload["exit_code"] is None
        assert "start" in start_payload["output"]
        session_id = start_payload["session_id"]

        poll_results = await tools.dispatch_tools(
            [_write_call("call-2", session_id, "", yield_time_ms=2500)],
            tmp_path,
        )
        poll_payload = _payload(poll_results[0])

        assert poll_payload["exit_code"] == 0
        assert "done" in poll_payload["output"]
        assert await process_manager.session_count() == 0

    asyncio.run(scenario())


def test_background_output_is_available_on_later_poll(tmp_path: Path) -> None:
    async def scenario() -> None:
        command = (
            f"{PYTHON} -u -c \"import time; print('start', flush=True); "
            "time.sleep(.3); print('background', flush=True); time.sleep(5)\""
        )
        start_results = await tools.dispatch_tools(
            [_exec_call("call-1", command, yield_time_ms=100)], tmp_path
        )
        session_id = _payload(start_results[0])["session_id"]
        await asyncio.sleep(0.6)

        poll_results = await tools.dispatch_tools(
            [_write_call("call-2", session_id, "", yield_time_ms=100)],
            tmp_path,
        )

        assert "background" in _payload(poll_results[0])["output"]

    asyncio.run(scenario())


def await_count() -> int:
    return asyncio.run(process_manager.session_count())


def test_interactive_stdin(tmp_path: Path) -> None:
    async def scenario() -> None:
        command = f"{PYTHON} -u -c \"name=input('Name: '); print('hi ' + name)\""
        start_results = await tools.dispatch_tools(
            [_exec_call("call-1", command, yield_time_ms=200)], tmp_path
        )
        session_id = _payload(start_results[0])["session_id"]

        write_results = await tools.dispatch_tools(
            [_write_call("call-2", session_id, "Bob\n", yield_time_ms=1000)],
            tmp_path,
        )
        payload = _payload(write_results[0])
        assert payload["exit_code"] == 0
        assert "hi Bob" in payload["output"]

    asyncio.run(scenario())


def test_same_session_write_calls_are_serialized(tmp_path: Path) -> None:
    async def scenario() -> None:
        command = (
            f"{PYTHON} -u -c \"import sys,time; print('ready', flush=True); "
            "line=sys.stdin.readline(); time.sleep(.2); print('one ' + line.strip(), flush=True); "
            "line=sys.stdin.readline(); print('two ' + line.strip(), flush=True)\""
        )
        start_results = await tools.dispatch_tools(
            [_exec_call("call-1", command, yield_time_ms=100)], tmp_path
        )
        session_id = _payload(start_results[0])["session_id"]

        results = await tools.dispatch_tools(
            [
                _write_call("call-2", session_id, "A\n", yield_time_ms=500),
                _write_call("call-3", session_id, "B\n", yield_time_ms=1000),
            ],
            tmp_path,
        )
        combined_output = "".join(_payload(result)["output"] for result in results)

        assert "one A" in combined_output
        assert "two B" in combined_output

    asyncio.run(scenario())


def test_apply_patch_still_works(tmp_path: Path) -> None:
    results = asyncio.run(
        tools.dispatch_tools(
            [
                _call(
                    "apply_patch",
                    "call-1",
                    {
                        "patch": (
                            "*** Begin Patch\n"
                            "*** Add File: created.txt\n"
                            "+hello\n"
                            "*** End Patch"
                        )
                    },
                )
            ],
            tmp_path,
        )
    )

    assert (tmp_path / "created.txt").read_text() == "hello\n"
    assert "Added" in results[0]["output"]


def test_writer_lock_blocks_concurrent_exec_starts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    def fake_patch(patch: str, workdir: Path) -> str:
        events.append("patch-start")
        time.sleep(0.2)
        events.append("patch-end")
        return "patched"

    async def fake_exec(
        command: str,
        cwd: Path,
        *,
        yield_time_ms: float,
        max_output_tokens: int,
    ) -> str:
        events.append("exec")
        return json.dumps({"exit_code": 0, "output": "ok", "wall_time_seconds": 0})

    monkeypatch.setattr(tools, "_apply_patch", fake_patch)
    monkeypatch.setattr(tools, "_run_exec_command", fake_exec)

    results = asyncio.run(
        tools.dispatch_tools(
            [
                _call(
                    "apply_patch",
                    "call-1",
                    {"patch": "*** Begin Patch\n*** End Patch"},
                ),
                _exec_call("call-2", "echo ok"),
            ],
            tmp_path,
        )
    )

    assert [result["call_id"] for result in results] == ["call-1", "call-2"]
    assert events == ["patch-start", "patch-end", "exec"]


def test_shell_command_remains_available(tmp_path: Path) -> None:
    results = asyncio.run(
        tools.dispatch_tools(
            [_call("shell_command", "call-1", {"command": "echo shell-ok"})],
            tmp_path,
        )
    )

    assert "shell-ok" in results[0]["output"]
    assert any(tool["name"] == "shell_command" for tool in tools.ALL_TOOLS)


def test_unknown_session_returns_model_correctable_error(tmp_path: Path) -> None:
    results = asyncio.run(
        tools.dispatch_tools([_write_call("call-1", 999999, "")], tmp_path)
    )

    assert results[0]["type"] == "function_call_output"
    assert "unknown session_id" in results[0]["output"]


def test_terminate_all_cleans_up_running_ptys(tmp_path: Path) -> None:
    results = asyncio.run(
        tools.dispatch_tools(
            [_exec_call("call-1", f"{PYTHON} -c \"import time; time.sleep(10)\"", yield_time_ms=100)],
            tmp_path,
        )
    )
    assert "session_id" in _payload(results[0])
    assert await_count() == 1

    asyncio.run(process_manager.terminate_all())

    assert await_count() == 0


def test_process_manager_lists_and_terminates_one_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        results = await tools.dispatch_tools(
            [
                _exec_call(
                    "call-1",
                    f"{PYTHON} -c \"import time; time.sleep(10)\"",
                    yield_time_ms=100,
                )
            ],
            tmp_path,
        )
        session_id = _payload(results[0])["session_id"]

        sessions = await process_manager.list_sessions()
        assert [session.session_id for session in sessions] == [session_id]
        assert sessions[0].alive

        assert await process_manager.terminate(session_id)
        assert await process_manager.session_count() == 0
        assert not await process_manager.terminate(session_id)

    asyncio.run(scenario())


def test_unknown_tool_returns_model_correctable_error(tmp_path: Path) -> None:
    results = asyncio.run(
        tools.dispatch_tools([_call("future_tool", "call-1", {})], tmp_path)
    )

    assert results[0]["type"] == "function_call_output"
    assert results[0]["output"].startswith("Error: unknown tool")
