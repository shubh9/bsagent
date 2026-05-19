# Simple PTY Unified Exec Slice

This is the smaller first implementation slice. It does not try to build full codex parity, but it should use a real PTY from the start. The purpose is to prove that bsagent can run simple managed terminal sessions concurrently, keep long-running sessions alive, and interact with them through stdin.

## Goal

Add a minimal PTY-backed unified exec implementation while keeping `shell_command` as a first-class tool.

This slice should prove:

- Multiple `exec_command` calls emitted in one model response can run concurrently in separate PTY sessions.
- `shell_command` remains available and useful for simple one-shot shell operations.
- `shell_command` can optionally run in parallel when config marks it parallel-safe.
- Parallel safety is controlled by config, not hard-coded tool names.
- Fast commands return output and exit codes.
- Long-running commands return a `session_id`.
- `write_stdin` can poll or interact with a running session.
- Different sessions can be polled concurrently, while the same session is protected by a per-session lock.
- `apply_patch` remains serialized and exclusive.
- Tests can verify concurrency without needing a real LLM call.

## Non-Goals

This slice intentionally does not include:

- `/ps` or `/stop`.
- Streaming output deltas.
- Full head/tail output buffering.
- LRU pruning.
- Process tree cleanup beyond basic termination.
- Sandboxing or permissions.
- Windows support.

Those belong in the full unified exec plan.

## Current State

`tools.py` currently has:

- `shell_command`
- `apply_patch`
- `_shell_lock = asyncio.Lock()`
- `dispatch_tools` executing tool calls in a sequential loop
- `_run_shell_command` using blocking `subprocess.run`

Because the tool calls are looped sequentially and the shell implementation blocks, batched commands cannot run in parallel.

## Target State

Add two new tools while keeping the existing shell tool:

- `shell_command`
- `exec_command`
- `write_stdin`
- `apply_patch`

`exec_command` starts a new PTY session every time. If the command exits during the initial yield window, return output and `exit_code` with no `session_id`. If it is still running, store it and return a `session_id`.

`write_stdin` is the only way to interact with an existing session. Passing empty `chars` means "poll for more output".

`shell_command` should stay because it has real utility: it is familiar, compact, and useful for simple one-shot command execution. In this slice it can either continue to use the existing implementation under the writer lock, or be routed through the same terminal backend as `exec_command` and become conditionally parallel-safe.

Important decision:

- Keep `shell_command` visible in `ALL_TOOLS`.
- Do not describe it as deprecated.
- Prefer `exec_command` for long-running or interactive commands that may need `session_id` / `write_stdin`.
- Allow `shell_command` to become parallel-safe when config includes it in `parallel_tools` and does not include it in `exclusive_tools`.
- Add explicit config for which tools are parallel-safe and which tools must remain exclusive.

### `exec_command` Schema

```json
{
  "name": "exec_command",
  "description": "Runs a command in a PTY, returning output or a session ID for ongoing interaction.",
  "parameters": {
    "type": "object",
    "properties": {
      "cmd": { "type": "string" },
      "workdir": { "type": "string" },
      "yield_time_ms": { "type": "number" },
      "max_output_tokens": { "type": "number" }
    },
    "required": ["cmd"]
  }
}
```

### `write_stdin` Schema

```json
{
  "name": "write_stdin",
  "description": "Write bytes to an existing PTY session, or pass empty chars to poll output.",
  "parameters": {
    "type": "object",
    "properties": {
      "session_id": { "type": "number" },
      "chars": { "type": "string" },
      "yield_time_ms": { "type": "number" },
      "max_output_tokens": { "type": "number" }
    },
    "required": ["session_id", "chars"]
  }
}
```

### Response Shape

Return JSON strings so the simple slice aligns with the full plan:

```json
{
  "exit_code": 0,
  "output": "hello\n",
  "wall_time_seconds": 0.12
}
```

For still-running processes:

```json
{
  "exit_code": null,
  "output": "server starting...\n",
  "wall_time_seconds": 0.5,
  "session_id": 1001
}
```

## Backend Decision

Use `pexpect` for this simple slice, but put it behind a backend interface immediately.

Dependencies:

```text
aiorwlock
pexpect
```

Suggested interface:

```python
class TerminalBackend:
    def spawn(self, command: str, cwd: Path) -> "TerminalSession":
        ...


class TerminalSession:
    def read_until_idle_or_exit(self, timeout_s: float) -> "TerminalRead":
        ...

    def write(self, data: str) -> None:
        ...

    def is_alive(self) -> bool:
        ...

    def exit_code(self) -> int | None:
        ...

    def terminate(self) -> None:
        ...
```

First concrete classes:

- `PexpectTerminalBackend`
- `PexpectTerminalSession`

Only these classes should import `pexpect`. The rest of the implementation should talk to the interface so the backend can later be replaced by `ptyprocess`, raw `pty`, or an exec server.

Because `pexpect` is blocking, run backend calls with `asyncio.to_thread(...)`.

## Minimal Process Manager

Add a small process manager now. This is what makes the PTY implementation real rather than just a one-off wrapper.

Suggested type:

```python
class ManagedTerminal:
    session_id: int
    command: str
    cwd: Path
    session: TerminalSession
    lock: asyncio.Lock
    last_used_at: float


class ProcessManager:
    async def allocate_id(self) -> int: ...
    async def store(self, managed: ManagedTerminal) -> None: ...
    async def get(self, session_id: int) -> ManagedTerminal | None: ...
    async def remove(self, session_id: int) -> None: ...
    async def terminate_all(self) -> None: ...
```

The simple slice can skip LRU pruning. It should still support `terminate_all` so tests and REPL shutdown can clean up live PTYs.

## Dispatch Model

For this slice, use `aiorwlock` so the design matches the full plan.

Add dependency:

```text
aiorwlock
```

Lock policy:

- `exec_command`: reader lock, can run in parallel with other sessions.
- `write_stdin`: reader lock, can run in parallel with other sessions.
- `apply_patch`: writer lock, exclusive.
- `shell_command`: conditionally reader or writer lock based on config.

Parallel policy should be data-driven:

- A config value defines which tools are allowed to run in parallel.
- A config value defines which tools are always exclusive.
- If a tool appears in both, exclusive wins.
- `apply_patch` must always be exclusive.
- `shell_command` can optionally be parallel-safe when config allows it.
- The decision should live in one helper, for example `tool_supports_parallel(name, config)`.

Suggested default for the simple slice:

```python
TOOL_PARALLEL_CONFIG = {
    "parallel_tools": {"exec_command", "write_stdin"},
    "exclusive_tools": {"apply_patch"},
}
```

Optional config when `shell_command` has been moved to the managed terminal backend:

```python
TOOL_PARALLEL_CONFIG = {
    "parallel_tools": {"shell_command", "exec_command", "write_stdin"},
    "exclusive_tools": {"apply_patch"},
}
```

Also add per-session locking:

- Each `ManagedTerminal` has its own `asyncio.Lock`.
- Every `write_stdin` call must acquire that session lock.
- This prevents two concurrent reads/writes from touching the same `pexpect.spawn` object.

Suggested dispatch shape:

```python
TOOL_PARALLEL_CONFIG = {
    "parallel_tools": {"exec_command", "write_stdin"},
    "exclusive_tools": {"apply_patch"},
}


def tool_supports_parallel(name: str) -> bool:
    if name in TOOL_PARALLEL_CONFIG["exclusive_tools"]:
        return False
    return name in TOOL_PARALLEL_CONFIG["parallel_tools"]

WRITE_TOOLS = {"apply_patch"}

async def dispatch_tools(tool_calls, workdir):
    async def dispatch_one(tc):
        name = tc.get("name", "")
        lock = (
            _execution_lock.reader_lock
            if tool_supports_parallel(name)
            else _execution_lock.writer_lock
        )
        async with lock:
            return await _run_one(tc, workdir)

    return list(await asyncio.gather(*(dispatch_one(tc) for tc in tool_calls)))
```

Important: `asyncio.gather` is required. An RwLock alone is not enough if the code still loops and awaits each tool one by one.

## Minimal Code Changes

### `requirements.txt`

Add:

```text
aiorwlock
pexpect
```

### New `unified_exec.py`

1. Define `TerminalBackend` and `TerminalSession`.
2. Implement `PexpectTerminalBackend`.
3. Implement `PexpectTerminalSession`.
4. Implement `ManagedTerminal`.
5. Implement `ProcessManager`.
6. Export one module-level `process_manager`.

Minimal `pexpect` behavior:

```python
child = pexpect.spawn(
    "/bin/bash",
    ["-lc", command],
    cwd=str(cwd),
    encoding="utf-8",
    timeout=timeout_s,
)
```

Reading:

```python
try:
    child.expect(pexpect.EOF, timeout=timeout_s)
    return TerminalRead(output=child.before, exit_code=child.exitstatus, alive=False)
except pexpect.TIMEOUT:
    return TerminalRead(output=child.before, exit_code=None, alive=True)
```

Writing:

```python
child.send(chars)
```

### `tools.py`

1. Import `time`, `aiorwlock`, and the process manager.
2. Replace `_shell_lock` with `_execution_lock = aiorwlock.RWLock()`.
3. Add `EXEC_COMMAND_TOOL`.
4. Add `WRITE_STDIN_TOOL`.
5. Keep `SHELL_COMMAND_TOOL` in `ALL_TOOLS`.
6. Change `ALL_TOOLS` to include `SHELL_COMMAND_TOOL`, `EXEC_COMMAND_TOOL`, `WRITE_STDIN_TOOL`, and `APPLY_PATCH_TOOL`.
7. Implement `_run_exec_command`.
8. Implement `_run_write_stdin`.
9. Add configurable `TOOL_PARALLEL_CONFIG`.
10. Add `tool_supports_parallel`.
11. Change `dispatch_tools` to `asyncio.gather`.
12. Add `_run_one` branches for `exec_command` and `write_stdin`.
13. Keep `apply_patch` under the writer lock.
14. Keep `_run_shell_command`; optionally route it through the managed terminal backend once the backend is stable.

Suggested `_run_exec_command` behavior:

```python
async def _run_exec_command(command: str, cwd: Path, yield_time_ms: float) -> str:
    start = time.monotonic()
    session_id = await process_manager.allocate_id()
    managed = await process_manager.spawn(session_id, command, cwd)
    read = await asyncio.to_thread(
        managed.session.read_until_idle_or_exit,
        yield_time_ms / 1000,
    )
    if read.alive:
        await process_manager.store(managed)
        return json.dumps({
            "exit_code": None,
            "output": truncate(read.output),
            "wall_time_seconds": round(time.monotonic() - start, 3),
            "session_id": session_id,
        })

    await process_manager.remove(session_id)
    return json.dumps({
        "exit_code": read.exit_code,
        "output": truncate(read.output),
        "wall_time_seconds": round(time.monotonic() - start, 3),
    })
```

Suggested `_run_write_stdin` behavior:

```python
async def _run_write_stdin(session_id: int, chars: str, yield_time_ms: float) -> str:
    managed = await process_manager.get(session_id)
    if managed is None:
        raise ToolCallError.respond_to_model(f"unknown session_id: {session_id}")

    async with managed.lock:
        if chars:
            await asyncio.to_thread(managed.session.write, chars)
        read = await asyncio.to_thread(
            managed.session.read_until_idle_or_exit,
            yield_time_ms / 1000,
        )

    if read.alive:
        return json.dumps({
            "exit_code": None,
            "output": truncate(read.output),
            "wall_time_seconds": ...,
            "session_id": session_id,
        })

    await process_manager.remove(session_id)
    return json.dumps({
        "exit_code": read.exit_code,
        "output": truncate(read.output),
        "wall_time_seconds": ...,
    })
```

### `loop.py`

Update the system prompt:

- Use `shell_command` for simple one-shot shell operations.
- Use `exec_command` when a command may be long-running, interactive, or needs ongoing `session_id` / `write_stdin` control.
- Independent `exec_command` calls may be batched in one response.
- Independent `shell_command` calls may also be batched when config marks `shell_command` parallel-safe.
- If `exec_command` returns `session_id`, use `write_stdin` to poll or interact with that session.
- Use `write_stdin` with empty `chars` to poll.
- Use `write_stdin` with text ending in `\n` to answer prompts.
- Use `apply_patch` for file edits.
- Avoid running conflicting file-writing commands in parallel.

## Testing Plan

Add tests that call `dispatch_tools` directly. Do not require an API call.

Suggested file:

- `tests/test_unified_exec_simple.py`

### Test 1: `pexpect` Backend Runs a Short Command

Use `PexpectTerminalBackend` directly.

Run:

```bash
echo hello
```

Expected:

- output includes `hello`
- process exits
- exit code is 0

### Test 2: Parallel PTY Commands Are Faster Than Sequential

Create three tool calls:

```python
python -c "import time; time.sleep(1); print('one')"
python -c "import time; time.sleep(1); print('two')"
python -c "import time; time.sleep(1); print('three')"
```

Call `dispatch_tools` with all three.

Expected:

- total wall time is less than 2 seconds
- all outputs are present
- all exit codes are 0

This proves they ran concurrently.

### Test 3: Fast Command Does Not Return `session_id`

Run:

```bash
echo hello
```

through `dispatch_tools`.

Expected:

- output includes `hello`
- result includes `exit_code`
- result does not include `session_id`

### Test 4: Long Command Returns `session_id`

Run:

```bash
python -c "import time; print('start'); time.sleep(5); print('done')"
```

with a short `yield_time_ms`.

Expected:

- output includes `start`
- result includes `session_id`
- exit code is null

### Test 5: Poll Session To Completion

Use the `session_id` from the long command.

Call `write_stdin` with:

```json
{ "chars": "", "yield_time_ms": 6000 }
```

Expected:

- output eventually includes `done`
- exit code is 0
- session is removed from the process manager

### Test 6: Interactive Stdin

Run:

```bash
python -c "name=input('Name: '); print('hi ' + name)"
```

Expected:

- initial `exec_command` returns a `session_id`
- `write_stdin` with `chars="Bob\n"` returns output containing `hi Bob`

### Test 7: Same-Session Calls Are Serialized

Create a running session, then schedule two `write_stdin` calls for the same `session_id`.

Expected:

- no concurrent access to the same `pexpect` child
- calls complete without output corruption or crashes

### Test 8: `apply_patch` Still Works

Use `dispatch_tools` with one `apply_patch` call that creates or edits a temporary file.

Expected:

- file content changes as expected
- result contains a successful patch message

### Test 9: Writer Lock Blocks Concurrent Exec Starts

Use one slow patch-like test helper if possible, or monkeypatch `_apply_patch` to sleep while holding the writer lock.

At the same time, schedule an `exec_command`.

Expected:

- exec does not complete until the writer-lock section finishes

This verifies the lock policy, not just subprocess behavior.

### Test 10: `shell_command` Remains Available

Call `dispatch_tools` with a `shell_command` tool call:

```bash
echo shell-ok
```

Expected:

- output includes `shell-ok`
- no `exec_command` / `write_stdin` behavior is required
- the tool remains model-visible

### Test 11: `shell_command` Parallel Policy Is Configurable

Test `tool_supports_parallel("shell_command")` in multiple configurations:

- default config without `shell_command` in `parallel_tools` -> `False`
- config with `shell_command` in `parallel_tools` -> `True`
- config with `shell_command` in both `parallel_tools` and `exclusive_tools` -> `False`

Expected:

- lock policy is not hard-coded by tool name alone
- config controls which tools can run in parallel
- exclusive config wins over parallel config

### Test 12: Unknown Session Returns Model-Correctable Error

Call `write_stdin` with a missing `session_id`.

Expected:

- result is `function_call_output`
- output explains `unknown session_id`

### Test 13: Terminate All Cleans Up Running PTYs

Start a long-running command, then call `process_manager.terminate_all()`.

Expected:

- process is terminated
- process store is empty

### Test 14: Unknown Tool Returns Model-Correctable Error

Pass a fake tool name.

Expected:

- result is `function_call_output`
- output starts with `Error: unknown tool`

## Manual Smoke Test

After tests pass, run a prompt like:

```text
Run three independent commands in parallel: sleep for 1 second and print A, B, and C.
```

Expected:

- model emits multiple `exec_command` calls in one response
- total runtime is close to 1 second rather than 3 seconds

If the model does not batch them naturally, prompt more explicitly:

```text
In one tool-call batch, run three independent exec_command calls that each sleep 1 second and print a different letter.
```

## Acceptance Criteria

- `pytest` passes.
- Three 1-second PTY commands complete in under 2 seconds through `dispatch_tools`.
- `pexpect` is hidden behind `TerminalBackend` / `TerminalSession`.
- `shell_command` remains present and tested.
- `shell_command` has configurable parallel-safety.
- the plan includes config for which tools are parallel and which are exclusive.
- Fast commands do not return `session_id`.
- Long-running commands return `session_id`.
- `write_stdin` can poll a session to completion.
- `write_stdin` can send interactive input.
- `apply_patch` remains exclusive.
- `exec_command` uses `pexpect` through the backend interface, not blocking `subprocess.run`.
- The code structure can be extended into the full managed process plan later.

## Final Step: Write and Run Tests

End this simple slice by writing and running tests. The slice is not complete until these tests pass.

Required final work:

1. Add `tests/test_unified_exec_simple.py`.
2. Cover backend behavior, parallel PTY commands, `shell_command` availability, configurable `shell_command` parallel policy, session polling, interactive stdin, same-session locking, cleanup, and `apply_patch` exclusivity.
3. Run:

```bash
python -m pytest tests
```

1. Fix failures before moving to the full plan.

