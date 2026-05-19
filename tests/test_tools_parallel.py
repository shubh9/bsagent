from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import tools


def _shell_call(call_id: str, command: str) -> dict[str, Any]:
    return {
        "name": "shell_command",
        "call_id": call_id,
        "arguments": json.dumps({"command": command}),
    }


def _patch_call(call_id: str, patch: str) -> dict[str, Any]:
    return {
        "name": "apply_patch",
        "call_id": call_id,
        "arguments": json.dumps({"patch": patch}),
    }


def _unknown_call(call_id: str) -> dict[str, Any]:
    return {
        "name": "future_write_tool",
        "call_id": call_id,
        "arguments": "{}",
    }


def _exec_call(call_id: str, command: str) -> dict[str, Any]:
    return {
        "name": "exec_command",
        "call_id": call_id,
        "arguments": json.dumps({"cmd": command}),
    }


DEFAULT_TOOL_PARALLEL_CONFIG = {
    "parallel_tools": {"exec_command", "write_stdin"},
    "exclusive_tools": {"apply_patch"},
}


@pytest.fixture(autouse=True)
def reset_tool_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "_tool_lock", tools.FairRWLock())
    monkeypatch.setattr(
        tools,
        "TOOL_PARALLEL_CONFIG",
        {
            "parallel_tools": set(DEFAULT_TOOL_PARALLEL_CONFIG["parallel_tools"]),
            "exclusive_tools": set(DEFAULT_TOOL_PARALLEL_CONFIG["exclusive_tools"]),
        },
    )


def test_tool_parallelism_registry_defaults_unknown_tools_to_exclusive() -> None:
    assert tools._tool_parallelism(_shell_call("call-1", "echo hi")) == "exclusive"
    assert tools._tool_parallelism(_exec_call("call-2", "echo hi")) == "parallel"
    assert tools._tool_parallelism(_patch_call("call-2", "*** Begin Patch\n*** End Patch")) == "exclusive"
    assert tools._tool_parallelism(_unknown_call("call-3")) == "exclusive"


def test_shell_command_parallelism_is_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not tools.tool_supports_parallel("shell_command")

    monkeypatch.setattr(
        tools,
        "TOOL_PARALLEL_CONFIG",
        {
            "parallel_tools": {"shell_command", "exec_command", "write_stdin"},
            "exclusive_tools": {"apply_patch"},
        },
    )
    assert tools.tool_supports_parallel("shell_command")

    monkeypatch.setattr(
        tools,
        "TOOL_PARALLEL_CONFIG",
        {
            "parallel_tools": {"shell_command", "exec_command", "write_stdin"},
            "exclusive_tools": {"shell_command", "apply_patch"},
        },
    )
    assert not tools.tool_supports_parallel("shell_command")


def test_shell_commands_run_in_parallel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        tools,
        "TOOL_PARALLEL_CONFIG",
        {
            "parallel_tools": {"shell_command", "exec_command", "write_stdin"},
            "exclusive_tools": {"apply_patch"},
        },
    )
    started = 0
    started_lock = threading.Lock()
    both_started = threading.Event()

    def fake_shell(command: str, cwd: Path, timeout: int) -> str:
        nonlocal started
        with started_lock:
            started += 1
            if started == 2:
                both_started.set()
        ran_in_parallel = both_started.wait(timeout=0.5)
        return f"exit=0\n{command}: parallel={ran_in_parallel}"

    monkeypatch.setattr(tools, "_run_shell_command", fake_shell)

    results = asyncio.run(
        tools.dispatch_tools(
            [
                _shell_call("call-1", "first"),
                _shell_call("call-2", "second"),
            ],
            tmp_path,
        )
    )

    assert [r["call_id"] for r in results] == ["call-1", "call-2"]
    assert "first: parallel=True" in results[0]["output"]
    assert "second: parallel=True" in results[1]["output"]


def test_shell_commands_run_serially_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    def fake_shell(command: str, cwd: Path, timeout: int) -> str:
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.12)
        with active_lock:
            active -= 1
        return f"exit=0\n{command}"

    monkeypatch.setattr(tools, "_run_shell_command", fake_shell)

    started = time.perf_counter()
    results = asyncio.run(
        tools.dispatch_tools(
            [
                _shell_call("call-1", "first"),
                _shell_call("call-2", "second"),
            ],
            tmp_path,
        )
    )
    elapsed = time.perf_counter() - started

    assert [r["call_id"] for r in results] == ["call-1", "call-2"]
    assert max_active == 1
    assert elapsed >= 0.20


def test_apply_patch_waits_for_active_shell_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shell_started = threading.Event()
    release_shell = threading.Event()
    patch_started = threading.Event()
    events: list[str] = []

    def fake_shell(command: str, cwd: Path, timeout: int) -> str:
        events.append("shell-start")
        shell_started.set()
        release_shell.wait(timeout=1)
        events.append("shell-end")
        return "exit=0"

    def fake_patch(patch: str, workdir: Path) -> str:
        patch_started.set()
        events.append("patch")
        return "patched"

    monkeypatch.setattr(tools, "_run_shell_command", fake_shell)
    monkeypatch.setattr(tools, "_apply_patch", fake_patch)

    async def run_dispatch() -> list[dict[str, Any]]:
        task = asyncio.create_task(
            tools.dispatch_tools(
                [
                    _shell_call("call-1", "slow"),
                    _patch_call("call-2", "*** Begin Patch\n*** End Patch"),
                ],
                tmp_path,
            )
        )
        assert await asyncio.to_thread(shell_started.wait, 1)
        await asyncio.sleep(0.05)
        assert not patch_started.is_set()
        release_shell.set()
        return await task

    results = asyncio.run(run_dispatch())

    assert [r["call_id"] for r in results] == ["call-1", "call-2"]
    assert events == ["shell-start", "shell-end", "patch"]


def test_writer_fairness_blocks_later_readers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_shell_started = threading.Event()
    release_first_shell = threading.Event()
    events: list[str] = []

    def fake_shell(command: str, cwd: Path, timeout: int) -> str:
        events.append(f"{command}-start")
        if command == "first":
            first_shell_started.set()
            release_first_shell.wait(timeout=1)
        events.append(f"{command}-end")
        return f"exit=0\n{command}"

    def fake_patch(patch: str, workdir: Path) -> str:
        events.append("patch")
        return "patched"

    monkeypatch.setattr(tools, "_run_shell_command", fake_shell)
    monkeypatch.setattr(tools, "_apply_patch", fake_patch)

    async def run_dispatch() -> list[dict[str, Any]]:
        task = asyncio.create_task(
            tools.dispatch_tools(
                [
                    _shell_call("call-1", "first"),
                    _patch_call("call-2", "*** Begin Patch\n*** End Patch"),
                    _shell_call("call-3", "second"),
                ],
                tmp_path,
            )
        )
        assert await asyncio.to_thread(first_shell_started.wait, 1)
        await asyncio.sleep(0.05)
        assert "second-start" not in events
        release_first_shell.set()
        return await task

    results = asyncio.run(run_dispatch())

    assert [r["call_id"] for r in results] == ["call-1", "call-2", "call-3"]
    assert events.index("patch") < events.index("second-start")


def test_recoverable_tool_error_releases_lock_for_waiting_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    patch_started = threading.Event()

    def fake_patch(patch: str, workdir: Path) -> str:
        patch_started.set()
        return "patched"

    monkeypatch.setattr(tools, "_apply_patch", fake_patch)

    results = asyncio.run(
        tools.dispatch_tools(
            [
                _shell_call("call-1", ""),
                _patch_call("call-2", "*** Begin Patch\n*** End Patch"),
            ],
            tmp_path,
        )
    )

    assert "requires a non-empty 'command'" in results[0]["output"]
    assert results[1]["output"] == "patched"
    assert patch_started.is_set()


def test_waiting_reader_cancellation_does_not_block_writer() -> None:
    async def scenario() -> list[str]:
        lock = tools.FairRWLock()
        events: list[str] = []
        release_reader = asyncio.Event()

        async def reader_1() -> None:
            async with lock.read():
                events.append("reader-1-start")
                await release_reader.wait()
                events.append("reader-1-end")

        async def writer() -> None:
            async with lock.write():
                events.append("writer")

        async def reader_2() -> None:
            async with lock.read():
                events.append("reader-2")

        reader_1_task = asyncio.create_task(reader_1())
        await asyncio.sleep(0)
        writer_task = asyncio.create_task(writer())
        await asyncio.sleep(0)
        reader_2_task = asyncio.create_task(reader_2())
        await asyncio.sleep(0)

        reader_2_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader_2_task

        release_reader.set()
        await asyncio.wait_for(asyncio.gather(reader_1_task, writer_task), timeout=1)
        return events

    assert asyncio.run(scenario()) == ["reader-1-start", "reader-1-end", "writer"]


def test_parallel_shells_are_faster_than_serial_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        tools,
        "TOOL_PARALLEL_CONFIG",
        {
            "parallel_tools": {"shell_command", "exec_command", "write_stdin"},
            "exclusive_tools": {"apply_patch"},
        },
    )

    def fake_shell(command: str, cwd: Path, timeout: int) -> str:
        time.sleep(0.2)
        return f"exit=0\n{command}"

    monkeypatch.setattr(tools, "_run_shell_command", fake_shell)

    started = time.perf_counter()
    results = asyncio.run(
        tools.dispatch_tools(
            [
                _shell_call("call-1", "one"),
                _shell_call("call-2", "two"),
                _shell_call("call-3", "three"),
            ],
            tmp_path,
        )
    )
    elapsed = time.perf_counter() - started

    assert [r["call_id"] for r in results] == ["call-1", "call-2", "call-3"]
    assert elapsed < 0.45
