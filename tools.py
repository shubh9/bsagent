"""
Tool definitions, dispatch, and the fair read/write execution gate.

Mirrors codex-rs tools/parallel.rs: tools that opt into parallel execution
hold a shared read lock, while side-effectful/exclusive tools hold a write
lock. shell_command conditionally opts into parallel execution; apply_patch
stays exclusive.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from collections.abc import Callable
from typing import Any, AsyncIterator, Literal

from unified_exec import process_manager


# ─── Error types ──────────────────────────────────────────────────────────────


class ToolCallError(Exception):
    """
    Mirrors codex-rs FunctionCallError.

    RespondToModel: the model sent bad arguments or made a recoverable mistake.
        The loop catches this, sends the message back as a function_call_output
        so the model can correct itself.

    Fatal: a system-level failure (disk I/O, programming error, unexpected
        state). The loop lets this propagate and crash — do not swallow it.
    """

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal

    @classmethod
    def respond_to_model(cls, message: str) -> "ToolCallError":
        return cls(message, fatal=False)

    @classmethod
    def fatal(cls, message: str) -> "ToolCallError":  # type: ignore[override]
        return cls(message, fatal=True)


# ─── Execution gate ───────────────────────────────────────────────────────────

class _RWWaiter:
    def __init__(self, mode: Literal["read", "write"]) -> None:
        self.mode = mode
        self.future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.acquired = False


class _RWLease:
    def __init__(self, lock: "FairRWLock", waiter: _RWWaiter) -> None:
        self._lock = lock
        self._waiter = waiter

    async def __aenter__(self) -> None:
        await self._lock._wait_for(self._waiter)

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._waiter.mode == "read":
            await self._lock.release_read()
        else:
            await self._lock.release_write()


class FairRWLock:
    """
    FIFO async read/write lock.

    Readers at the front of the queue run together. Writers run alone. Once a
    writer is queued, later readers cannot jump ahead of it, which prevents a
    stream of shell commands from starving apply_patch.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._queue: deque[_RWWaiter] = deque()
        self._active_readers = 0
        self._writer_active = False

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        async with await self.reserve_read():
            yield

    @asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        async with await self.reserve_write():
            yield

    async def reserve_read(self) -> _RWLease:
        return _RWLease(self, await self._enqueue("read"))

    async def reserve_write(self) -> _RWLease:
        return _RWLease(self, await self._enqueue("write"))

    async def acquire_read(self) -> None:
        waiter = await self._enqueue("read")
        await self._wait_for(waiter)

    async def acquire_write(self) -> None:
        waiter = await self._enqueue("write")
        await self._wait_for(waiter)

    async def release_read(self) -> None:
        async with self._lock:
            if self._active_readers <= 0:
                raise RuntimeError("release_read called without an active reader")
            self._active_readers -= 1
            if self._active_readers == 0:
                self._wake_waiters_unlocked()

    async def release_write(self) -> None:
        async with self._lock:
            if not self._writer_active:
                raise RuntimeError("release_write called without an active writer")
            self._writer_active = False
            self._wake_waiters_unlocked()

    async def _enqueue(self, mode: Literal["read", "write"]) -> _RWWaiter:
        waiter = _RWWaiter(mode)
        async with self._lock:
            self._queue.append(waiter)
            self._wake_waiters_unlocked()
        return waiter

    async def _wait_for(self, waiter: _RWWaiter) -> None:
        try:
            await waiter.future
        except BaseException:
            async with self._lock:
                if waiter.acquired:
                    if waiter.mode == "read":
                        self._active_readers -= 1
                        if self._active_readers == 0:
                            self._wake_waiters_unlocked()
                    else:
                        self._writer_active = False
                        self._wake_waiters_unlocked()
                else:
                    try:
                        self._queue.remove(waiter)
                    except ValueError:
                        pass
                    self._wake_waiters_unlocked()
            raise

    def _wake_waiters_unlocked(self) -> None:
        if self._writer_active or self._active_readers > 0:
            while self._queue and self._queue[0].mode == "read":
                waiter = self._queue.popleft()
                waiter.acquired = True
                self._active_readers += 1
                if not waiter.future.done():
                    waiter.future.set_result(None)
            return

        if not self._queue:
            return

        if self._queue[0].mode == "write":
            waiter = self._queue.popleft()
            waiter.acquired = True
            self._writer_active = True
            if not waiter.future.done():
                waiter.future.set_result(None)
            return

        while self._queue and self._queue[0].mode == "read":
            waiter = self._queue.popleft()
            waiter.acquired = True
            self._active_readers += 1
            if not waiter.future.done():
                waiter.future.set_result(None)


_tool_lock = FairRWLock()

# ─── Tool parallelism policy ---------------------------------------------------

ToolParallelism = Literal["parallel", "exclusive"]
ToolParallelismPolicy = ToolParallelism | Callable[[], ToolParallelism]

TOOL_PARALLEL_CONFIG: dict[str, set[str]] = {
    "parallel_tools": {"exec_command", "write_stdin"},
    "exclusive_tools": {"apply_patch"},
}


def _env_flag_enabled(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _shell_command_parallelism() -> ToolParallelism:
    if "shell_command" in TOOL_PARALLEL_CONFIG["exclusive_tools"]:
        return "exclusive"
    env_value = os.environ.get("BSAGENT_PARALLEL_SHELL_COMMANDS")
    if env_value is not None:
        return (
            "parallel"
            if _env_flag_enabled("BSAGENT_PARALLEL_SHELL_COMMANDS", default=False)
            else "exclusive"
        )
    if not _env_flag_enabled("BSAGENT_PARALLEL_SHELL_COMMANDS", default=True):
        return "exclusive"
    return (
        "parallel"
        if "shell_command" in TOOL_PARALLEL_CONFIG["parallel_tools"]
        else "exclusive"
    )


def _configured_tool_parallelism(tool_name: str) -> ToolParallelism:
    if tool_name in TOOL_PARALLEL_CONFIG["exclusive_tools"]:
        return "exclusive"
    if tool_name in TOOL_PARALLEL_CONFIG["parallel_tools"]:
        return "parallel"
    return "exclusive"


# Unknown tools default to exclusive. This keeps new side-effectful tools safe
# until they explicitly opt into parallel execution.
TOOL_PARALLELISM: dict[str, ToolParallelismPolicy] = {
    "shell_command": _shell_command_parallelism,
    "exec_command": lambda: _configured_tool_parallelism("exec_command"),
    "write_stdin": lambda: _configured_tool_parallelism("write_stdin"),
    "apply_patch": lambda: _configured_tool_parallelism("apply_patch"),
}

# ─── Tool schemas (exact codex names and parameter shapes) ───────────────────

SHELL_COMMAND_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "shell_command",
    "description": (
        "Run a shell command in the working directory and return combined "
        "stdout + stderr. Use for reading files, running tests, searching "
        "code, installing packages, or any other shell operation. The runtime "
        "can execute multiple shell_command calls from the same assistant "
        "response concurrently when they are independent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute.",
            },
            "workdir": {
                "type": "string",
                "description": (
                    "Working directory for the command, relative to the "
                    "agent working directory. Defaults to the agent workdir."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds. Defaults to 60.",
            },
        },
        "required": ["command"],
    },
}

EXEC_COMMAND_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "exec_command",
    "description": (
        "Run a shell command in a managed PTY. Fast commands return output "
        "directly; long-running commands return a session_id for polling or "
        "interactive input via write_stdin."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "cmd": {"type": "string", "description": "Shell command to execute."},
            "workdir": {
                "type": "string",
                "description": "Working directory relative to the agent workdir.",
            },
            "yield_time_ms": {
                "type": "integer",
                "description": "Milliseconds to wait for output before yielding.",
            },
            "max_output_tokens": {
                "type": "integer",
                "description": "Approximate max output tokens to return.",
            },
        },
        "required": ["cmd"],
    },
}

WRITE_STDIN_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "write_stdin",
    "description": "Write characters to an existing exec_command session and return recent output.",
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "integer", "description": "Managed PTY session id."},
            "chars": {"type": "string", "description": "Characters to write; empty string polls."},
            "yield_time_ms": {
                "type": "integer",
                "description": "Milliseconds to wait for output before yielding.",
            },
            "max_output_tokens": {
                "type": "integer",
                "description": "Approximate max output tokens to return.",
            },
        },
        "required": ["session_id"],
    },
}

APPLY_PATCH_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "apply_patch",
    "description": (
        "Apply a structured patch to create, update, or delete files. "
        "Prefer this over shell redirection for file edits — it is safer "
        "and shows a clean diff. Use the codex patch format:\n\n"
        "*** Begin Patch\n"
        "*** Update File: path/to/file\n"
        "@@\n"
        "-old line\n"
        "+new line\n"
        "*** End Patch\n\n"
        "Supports: Update File, Add File, Delete File."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "description": "Patch string in codex patch format.",
            },
        },
        "required": ["patch"],
    },
}

ALL_TOOLS = [SHELL_COMMAND_TOOL, EXEC_COMMAND_TOOL, WRITE_STDIN_TOOL, APPLY_PATCH_TOOL]

# ─── Tool implementations ─────────────────────────────────────────────────────

OUTPUT_CAP = 8_000  # chars — mirrors codex DEFAULT_OUTPUT_BYTES_CAP


def _run_shell_command(command: str, cwd: Path, timeout: int) -> str:
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (result.stdout or "") + (result.stderr or "")
        if len(out) > OUTPUT_CAP:
            out = out[:OUTPUT_CAP] + f"\n... [truncated, {len(out) - OUTPUT_CAP} more chars]"
        return f"exit={result.returncode}\n{out}" if out else f"exit={result.returncode}"
    except subprocess.TimeoutExpired:
        return f"[timeout after {timeout}s]"


def _cap_text(text: str, max_output_tokens: int | None = None) -> str:
    cap = OUTPUT_CAP
    if max_output_tokens is not None:
        cap = min(cap, max_output_tokens * APPROX_CHARS_PER_TOKEN)
    if len(text) > cap:
        return text[:cap] + f"\n... [truncated, {len(text) - cap} more chars]"
    return text


APPROX_CHARS_PER_TOKEN = 4


async def _run_exec_command(
    command: str,
    cwd: Path,
    *,
    yield_time_ms: float,
    max_output_tokens: int,
) -> str:
    started = time.monotonic()
    session_id = await process_manager.allocate_id()
    managed = await process_manager.spawn(session_id, command, cwd)
    read = await asyncio.to_thread(
        managed.session.read_until_idle_or_exit,
        yield_time_ms / 1000,
    )
    payload: dict[str, Any] = {
        "exit_code": read.exit_code,
        "output": _cap_text(read.output, max_output_tokens),
        "wall_time_seconds": round(time.monotonic() - started, 3),
    }
    if read.alive:
        await process_manager.store(managed)
        payload["session_id"] = session_id
    return json.dumps(payload)


async def _run_write_stdin(
    session_id: int,
    chars: str,
    *,
    yield_time_ms: float,
    max_output_tokens: int,
) -> str:
    started = time.monotonic()
    managed = await process_manager.get(session_id)
    if managed is None:
        raise ToolCallError.respond_to_model(f"unknown session_id: {session_id}")

    async with managed.lock:
        await asyncio.to_thread(managed.session.write, chars)
        read = await asyncio.to_thread(
            managed.session.read_until_idle_or_exit,
            yield_time_ms / 1000,
        )
        payload: dict[str, Any] = {
            "exit_code": read.exit_code,
            "output": _cap_text(read.output, max_output_tokens),
            "wall_time_seconds": round(time.monotonic() - started, 3),
        }
        if read.alive:
            payload["session_id"] = session_id
        else:
            await process_manager.remove(session_id)
        return json.dumps(payload)


def _apply_patch(patch: str, workdir: Path) -> str:
    """
    Parse and apply a codex-format patch.

    Supported operations:
      *** Add File: <path>        — create file with lines that follow
      *** Delete File: <path>     — remove file
      *** Update File: <path>     — apply @@ hunk(s) to existing file
    """
    lines = patch.splitlines()
    # Strip the Begin/End Patch envelope
    if lines and lines[0].strip() == "*** Begin Patch":
        lines = lines[1:]
    if lines and lines[-1].strip() == "*** End Patch":
        lines = lines[:-1]

    messages: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.startswith("*** Add File:"):
            path = workdir / line[len("*** Add File:"):].strip()
            i += 1
            content_lines: list[str] = []
            while i < len(lines) and not lines[i].startswith("***"):
                line_content = lines[i]
                content_lines.append(
                    line_content[1:] if line_content.startswith("+") else line_content
                )
                i += 1
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(content_lines) + "\n", encoding="utf-8")
            messages.append(f"Added {path}")

        elif line.startswith("*** Delete File:"):
            path = workdir / line[len("*** Delete File:"):].strip()
            i += 1
            if path.exists():
                path.unlink()
                messages.append(f"Deleted {path}")
            else:
                messages.append(f"Not found (already deleted?): {path}")

        elif line.startswith("*** Update File:"):
            path = workdir / line[len("*** Update File:"):].strip()
            i += 1
            if not path.exists():
                messages.append(f"ERROR: file not found: {path}")
                # skip until next *** directive
                while i < len(lines) and not lines[i].startswith("***"):
                    i += 1
                continue

            file_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            hunks_applied = 0

            while i < len(lines) and lines[i].strip() == "@@":
                i += 1  # skip @@
                removals: list[str] = []
                additions: list[str] = []
                context: list[str] = []

                while i < len(lines) and not lines[i].startswith("***") and lines[i].strip() != "@@":
                    h = lines[i]
                    if h.startswith("-"):
                        removals.append(h[1:])
                    elif h.startswith("+"):
                        additions.append(h[1:])
                    else:
                        context.append(h)
                    i += 1

                # Find removal block in file and replace
                if removals:
                    for start in range(len(file_lines)):
                        end = start + len(removals)
                        if file_lines[start:end] == removals:
                            file_lines[start:end] = additions
                            hunks_applied += 1
                            break
                    else:
                        messages.append(f"WARNING: hunk not found in {path}")
                else:
                    # Pure addition — append to file
                    file_lines.extend(additions)
                    hunks_applied += 1

            path.write_text("\n".join(file_lines) + "\n", encoding="utf-8")
            messages.append(f"Updated {path} ({hunks_applied} hunk(s) applied)")
        else:
            i += 1

    return "\n".join(messages) if messages else "Patch applied (no changes reported)"


# ─── Dispatch ─────────────────────────────────────────────────────────────────


async def dispatch_tools(
    tool_calls: list[dict[str, Any]],
    workdir: Path,
) -> list[dict[str, Any]]:
    """
    Execute tool calls under a fair read/write lock.

    shell_command holds a shared read lock, so independent shell calls from
    the same model response can run concurrently. apply_patch and unknown
    tools hold the exclusive write lock, so they block all other tool I/O.

    ToolCallError.respond_to_model is caught here and returned as a
    function_call_output so the model can see and correct its mistake.
    Any other exception (including ToolCallError with fatal=True) propagates
    and crashes the process — do not swallow unexpected failures.
    """
    # Reserve lock positions in model-output order before starting tasks.
    # This keeps the RW lock fair even if asyncio schedules tasks differently.
    lock_contexts = [
        await (
            _tool_lock.reserve_read()
            if _supports_parallel_tool_calls(tc)
            else _tool_lock.reserve_write()
        )
        for tc in tool_calls
    ]
    tasks = [
        asyncio.create_task(_run_with_lock(tc, workdir, lock_context))
        for tc, lock_context in zip(tool_calls, lock_contexts)
    ]
    return await asyncio.gather(*tasks)


def _tool_parallelism(tc: dict[str, Any]) -> ToolParallelism:
    policy = TOOL_PARALLELISM.get(tc.get("name", ""), "exclusive")
    return policy() if callable(policy) else policy


def tool_supports_parallel(tool_name: str) -> bool:
    return _tool_parallelism({"name": tool_name}) == "parallel"


def _supports_parallel_tool_calls(tc: dict[str, Any]) -> bool:
    return _tool_parallelism(tc) == "parallel"


async def _run_with_lock(
    tc: dict[str, Any],
    workdir: Path,
    lock_context: _RWLease,
) -> dict[str, Any]:
    async with lock_context:
        return await _run_one_safely(tc, workdir)


async def _run_one_safely(tc: dict[str, Any], workdir: Path) -> dict[str, Any]:
    call_id: str = tc.get("call_id", tc.get("id", ""))
    try:
        return await _run_one(tc, workdir)
    except ToolCallError as exc:
        if exc.fatal:
            raise
        return {
            "type": "function_call_output",
            "call_id": call_id,
            "output": f"Error: {exc}",
        }


async def _run_one(tc: dict[str, Any], workdir: Path) -> dict[str, Any]:
    name: str = tc["name"]

    raw_arguments = tc.get("arguments", "{}")
    try:
        args: dict[str, Any] = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ToolCallError.respond_to_model(
            f"failed to parse function arguments for {name!r}: {exc}"
        ) from exc

    call_id: str = tc.get("call_id", tc.get("id", ""))

    if name == "shell_command":
        command = args.get("command")
        if not command:
            raise ToolCallError.respond_to_model(
                "shell_command requires a non-empty 'command' argument"
            )
        cwd = workdir
        if "workdir" in args:
            candidate = Path(args["workdir"])
            cwd = candidate if candidate.is_absolute() else (workdir / candidate)
        timeout = int(args.get("timeout", 60))
        output = await asyncio.to_thread(_run_shell_command, command, cwd, timeout)

    elif name == "exec_command":
        command = args.get("cmd")
        if not command:
            raise ToolCallError.respond_to_model(
                "exec_command requires a non-empty 'cmd' argument"
            )
        cwd = workdir
        if "workdir" in args:
            candidate = Path(args["workdir"])
            cwd = candidate if candidate.is_absolute() else (workdir / candidate)
        output = await _run_exec_command(
            command,
            cwd,
            yield_time_ms=float(args.get("yield_time_ms", 1000)),
            max_output_tokens=int(args.get("max_output_tokens", 2_000)),
        )

    elif name == "write_stdin":
        session_id = args.get("session_id")
        if session_id is None:
            raise ToolCallError.respond_to_model(
                "write_stdin requires a 'session_id' argument"
            )
        output = await _run_write_stdin(
            int(session_id),
            str(args.get("chars", "")),
            yield_time_ms=float(args.get("yield_time_ms", 1000)),
            max_output_tokens=int(args.get("max_output_tokens", 2_000)),
        )

    elif name == "apply_patch":
        patch = args.get("patch")
        if not patch:
            raise ToolCallError.respond_to_model(
                "apply_patch requires a non-empty 'patch' argument"
            )
        output = _apply_patch(patch, workdir)

    else:
        raise ToolCallError.respond_to_model(f"unknown tool: {name!r}")

    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": output,
    }
