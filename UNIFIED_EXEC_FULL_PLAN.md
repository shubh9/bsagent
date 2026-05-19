# Full Unified Exec Implementation Plan

This plan describes how to evolve the current bsagent tool runtime from:

- `shell_command` implemented with blocking `subprocess.run`
- `apply_patch` implemented as a direct file mutator
- one global `asyncio.Lock` serializing all tool calls

into a codex-style tool runtime with both `shell_command` and unified exec support: parallel process execution, managed background sessions, stdin reuse, output buffering, and explicit cleanup.

## Goals

- Keep `shell_command` as a first-class tool.
- Add `exec_command`, a managed process tool for long-running or interactive commands.
- Add `write_stdin`, used to poll or interact with an existing running process.
- Allow multiple `exec_command` calls to run concurrently when the model batches tool calls.
- Allow `shell_command` to become parallel-safe conditionally through config.
- Add config for which tools can run in parallel and which tools must remain exclusive.
- Keep `apply_patch` exclusive, because it mutates files.
- Return a `session_id` only when a process is still alive after the initial yield window.
- Remove finished commands from the process store automatically.
- Support long-running commands such as dev servers, test watchers, `tail -f`, and interactive prompts.
- Add cleanup paths for process exit, REPL shutdown, and an explicit stop command.
- Put terminal spawning behind a backend interface.
- Use `pexpect` as the first PTY backend, while keeping the design replaceable with `ptyprocess`, native `pty`, or an exec server later.

## Current Behavior

`tools.py` currently has two model-visible tools:

- `shell_command(command, workdir?, timeout?)`
- `apply_patch(patch)`

`dispatch_tools` loops through model tool calls one by one. Each call takes `_shell_lock`, so even if the model emits multiple tool calls in one response, they execute sequentially.

This is safe, but it leaves performance and long-running process control on the table.

## Target Behavior

The target model-visible tools should be:

- `shell_command`
- `exec_command`
- `write_stdin`
- `apply_patch`

`exec_command` always starts a new managed process. If the command exits during the initial wait window, the process is removed and the response includes `exit_code` but no `session_id`. If the command is still alive, the response includes a `session_id` for later `write_stdin` calls.

`write_stdin` is the only way to interact with an existing process. The runtime should not automatically route a new command to an old session.

`shell_command` remains a useful direct command tool. It should not be removed or treated as deprecated. Its implementation may remain simple and serialized at first, then become conditionally parallel-safe when config allows it.

`apply_patch` remains exclusive. It should block concurrent process starts while it is applying a patch, and running processes should not prevent the model from later polling them.

## Architecture

### 1. Process Manager

Create a process manager responsible for all live command sessions.

Suggested module:

- `unified_exec.py`

Suggested main types:

- `ProcessManager`
- `ManagedProcess`
- `OutputBuffer`
- `ExecCommandResult`
- `TerminalBackend`
- `TerminalSession`

Responsibilities:

- Allocate unique numeric `session_id` values.
- Start commands.
- Store live processes.
- Remove exited processes.
- Kill one process.
- Kill all processes.
- Prune old processes when the cap is reached.

Suggested constants:

```python
DEFAULT_YIELD_TIME_MS = 10_000
DEFAULT_WRITE_STDIN_YIELD_TIME_MS = 250
DEFAULT_EMPTY_POLL_YIELD_TIME_MS = 5_000
MAX_BACKGROUND_POLL_TIME_MS = 300_000
MAX_PROCESSES = 64
OUTPUT_MAX_BYTES = 1024 * 1024
OUTPUT_CAP = 8_000
```

Suggested parallel-tool config:

```python
TOOL_PARALLEL_CONFIG = {
    "parallel_tools": {"exec_command", "write_stdin"},
    "exclusive_tools": {"apply_patch"},
}
```

If `shell_command` is configured to run in parallel:

```python
TOOL_PARALLEL_CONFIG = {
    "parallel_tools": {"shell_command", "exec_command", "write_stdin"},
    "exclusive_tools": {"apply_patch"},
}
```

Rules:

- Tools in `parallel_tools` use the reader lock.
- Tools in `exclusive_tools` use the writer lock.
- If a tool appears in both, exclusive wins.
- `apply_patch` must always be exclusive.

### 2. Terminal Backend Interface

Do not wire `pexpect` directly throughout the tool code. Hide it behind a small interface so the process manager owns terminal lifecycle concepts while the backend owns spawn/read/write/terminate details.

Suggested interface:

```python
class TerminalBackend(Protocol):
    def spawn(self, command: str, cwd: Path, *, env: dict[str, str] | None = None) -> TerminalSession:
        ...


class TerminalSession(Protocol):
    def read_until_idle_or_exit(self, timeout_s: float) -> TerminalRead:
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

First implementation:

- `PexpectTerminalBackend`
- `PexpectTerminalSession`

The `pexpect` backend should be the only place that imports `pexpect`. Because `pexpect` is blocking, call backend methods through `asyncio.to_thread(...)` from the async process manager.

Why this matters:

- `pexpect` gets us a real PTY quickly on macOS/Linux.
- The rest of the runtime should not depend on `pexpect` APIs like `child.before`, `child.expect`, or `child.send`.
- If scaling, Windows, or output streaming requirements change later, the backend can be swapped without rewriting tool schemas or process management.

### 3. Managed Process

`ManagedProcess` should wrap one live process and its output state.

Fields:

- `session_id: int`
- `command: str`
- `cwd: Path`
- `session: TerminalSession`
- `started_at: float`
- `last_used_at: float`
- `output_buffer: OutputBuffer`
- `exit_code: int | None`
- `reader_tasks: list[asyncio.Task]`
- `exit_task: asyncio.Task`
- `tty: bool`

Responsibilities:

- Read stdout/stderr continuously.
- Append output into a bounded buffer.
- Signal waiters when new output arrives.
- Track exit status.
- Support stdin writes if stdin is open.
- Terminate gracefully, then kill if needed.

### 4. Output Buffer

Use a bounded head/tail buffer instead of unbounded strings.

Minimum viable version:

- Keep the latest `OUTPUT_MAX_BYTES`.
- Track whether older output was truncated.
- Return snapshots capped to `max_output_tokens` or `OUTPUT_CAP`.

Codex-like version:

- Preserve head and tail, dropping the middle.
- Track original byte/token count.
- Return structured truncation notes.

### 5. Tool Dispatch Locking

Replace the single `asyncio.Lock` with an async read-write lock.

Add dependency:

```text
aiorwlock
```

Lock policy:

- `exec_command`: reader lock, parallel-safe.
- `write_stdin`: reader lock, parallel-safe.
- `shell_command`: conditionally reader or writer lock based on config.
- `apply_patch`: writer lock, exclusive.

Why:

- Multiple commands may run at the same time.
- `apply_patch` directly mutates files and should not race with tool-start decisions.

Important nuance:

Running background processes may still write files after `exec_command` returns. The writer lock cannot prevent that. This is the same class of risk codex accepts for long-running commands. The model must avoid intentionally running conflicting commands in parallel.

Tool lock behavior should use a helper such as `tool_supports_parallel(name, config)` instead of hard-coding tool names. Initially:

- `shell_command` not listed in `parallel_tools` -> writer lock.
- `shell_command` listed in `parallel_tools` -> reader lock.
- `shell_command` listed in `exclusive_tools` -> writer lock, even if also listed in `parallel_tools`.
- `exec_command` and `write_stdin` -> reader lock.
- `apply_patch` -> writer lock.

Add per-session locks too:

- Different terminal sessions may be polled or written in parallel.
- The same `session_id` must be protected by a per-session `asyncio.Lock`.
- Two concurrent `write_stdin` calls to the same PTY are not safe.

### 6. `exec_command` Tool

Suggested schema:

```json
{
  "name": "exec_command",
  "description": "Runs a command, returning output or a session ID for ongoing interaction.",
  "parameters": {
    "type": "object",
    "properties": {
      "cmd": { "type": "string" },
      "workdir": { "type": "string" },
      "yield_time_ms": { "type": "number" },
      "max_output_tokens": { "type": "number" },
      "tty": { "type": "boolean" }
    },
    "required": ["cmd"]
  }
}
```

Execution flow:

1. Allocate `session_id`.
2. Start process through the configured `TerminalBackend`.
3. Read output through the terminal backend and append it to the managed output buffer.
4. Wait until either:
   - process exits, or
   - `yield_time_ms` expires.
5. If exited:
   - collect output
   - remove process from the store
   - return `exit_code`
   - omit `session_id`
6. If still running:
   - store process
   - return output so far
   - include `session_id`

Response shape:

```json
{
  "output": "text",
  "wall_time_seconds": 1.25,
  "session_id": 1234,
  "exit_code": null,
  "original_token_count": 900
}
```

For completed processes:

```json
{
  "output": "text",
  "wall_time_seconds": 0.18,
  "exit_code": 0,
  "original_token_count": 120
}
```

### 7. `shell_command` Tool

Keep the existing `shell_command` schema and model-visible tool. It remains useful for simple one-shot commands and mirrors the way codex can expose shell and unified exec behavior depending on configuration.

Implementation options:

1. Keep the current blocking implementation under the writer lock.
2. Route `shell_command` through the managed terminal backend and mark it parallel-safe in config.
3. Support both modes through configuration, with `tool_supports_parallel("shell_command", config)` reflecting the active config.

Behavioral distinction:

- `shell_command` is for direct one-shot shell work.
- `exec_command` is for managed sessions that may return `session_id`.
- `write_stdin` only talks to `exec_command` sessions.

Testing should prove `shell_command` remains available and that its parallel lock policy is configurable.

### 8. `write_stdin` Tool

Suggested schema:

```json
{
  "name": "write_stdin",
  "description": "Write bytes to an existing exec session, or pass empty chars to poll output.",
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

Execution flow:

1. Look up `session_id`.
2. If unknown, return a model-correctable error.
3. If `chars` is non-empty:
   - write to stdin
   - flush/drain
4. Wait for new output, process exit, or timeout.
5. If process exited:
   - return output and `exit_code`
   - remove process from store
6. If still running:
   - return output and keep `session_id`

Empty `chars` means "poll".

### 9. PTY Backend: `pexpect` First

Use `pexpect` for the first real PTY backend.

PTY behavior is needed for:

- interactive CLIs
- prompts that require a TTY
- curses-style output
- tools that change behavior when stdout is a terminal

`pexpect` gives:

- real PTY sessions on macOS/Linux
- straightforward `send`, EOF, and timeout behavior
- a small implementation surface for the first backend

`pexpect` tradeoffs:

- blocking API, so wrap backend calls in `asyncio.to_thread`
- weak Windows story
- no built-in head/tail buffer, lifecycle event model, or process pruning
- requires per-session locks because a `pexpect.spawn` object cannot be read/written concurrently

Future backend options:

- `ptyprocess` for lower-level control while staying above raw PTY APIs
- raw `pty.openpty()` plus `asyncio` readers for more async-native behavior
- an external exec server if this runtime needs to become reusable outside bsagent

### 10. Cleanup Semantics

Cleanup must happen in all of these cases:

- Process exits naturally.
- `write_stdin` observes process exit.
- Reader or exit watcher observes process exit.
- User exits REPL.
- User runs a stop command.
- Store reaches `MAX_PROCESSES` and prunes.
- Process start fails after ID allocation.

Suggested explicit commands:

- `/ps`: list background sessions.
- `/stop`: terminate all background sessions.
- `/stop <session_id>`: terminate one session.

Prune policy:

1. Prefer removing exited processes.
2. Preserve the most recently used N processes.
3. If all are live and over cap, terminate least recently used.

### 11. System Prompt Update

Update the prompt to teach the model:

- Use `shell_command` for simple one-shot shell work.
- Use `exec_command` for commands that may be long-running, interactive, or need ongoing `session_id` / `write_stdin` control.
- `exec_command` starts a new process every time.
- Use `write_stdin(session_id, chars="")` to poll a still-running process.
- Use `write_stdin(session_id, chars="...\n")` to answer prompts.
- Batch independent commands when useful.
- Batch independent `shell_command` calls only when config marks them parallel-safe.
- Do not run conflicting file-writing commands in parallel.
- Use `apply_patch` for direct file edits.

### 12. `shell_command` Retention Strategy

Keep `shell_command` visible and useful. Do not remove it as part of unified exec.

Suggested rollout:

1. Add `exec_command` and `write_stdin`.
2. Keep `shell_command` in `ALL_TOOLS`.
3. Add `TOOL_PARALLEL_CONFIG`.
4. Add `tool_supports_parallel`.
5. Keep `shell_command` writer-locked unless config explicitly allows it.
6. Add an optional managed-terminal backend for `shell_command`.
7. Mark `shell_command` parallel-safe only when config includes it in `parallel_tools` and not in `exclusive_tools`.

### 13. Testing Plan

Unit tests:

- `PexpectTerminalBackend` can run a short command.
- `PexpectTerminalBackend` can keep a long-running command alive.
- `shell_command` remains model-visible.
- `shell_command` works for simple one-shot commands.
- `shell_command` parallel policy is configurable.
- exclusive tool config wins over parallel tool config.
- `exec_command` completes fast command and returns no `session_id`.
- `exec_command` for a long command returns `session_id`.
- `write_stdin` with empty chars polls a running process.
- `write_stdin` with input sends text to a process.
- Process exits and is removed from the store.
- Unknown `session_id` returns a model-correctable error.
- `terminate_all` kills live processes.
- Store cap prunes old entries.
- `apply_patch` still runs exclusively.

Integration tests:

- Five `grep` or `python -c` commands run in parallel.
- Parallel-safe `shell_command` calls run concurrently when config allows it.
- One long command runs while a short command completes.
- A simple interactive process receives stdin.
- REPL shutdown terminates a background process.
- Output truncation works for large output.
- Same-session `write_stdin` calls are serialized by a per-session lock.

Manual smoke tests:

```bash
python -m pytest tests
bsagent "run five independent sleep/echo commands in parallel"
bsagent "start a long-running python loop, poll it twice, then stop it"
```

## Implementation Milestones

### Milestone 1: Process Manager Skeleton

- Add `unified_exec.py`.
- Add ID allocation and process store.
- Add terminate-all support.
- No model tool changes yet.

### Milestone 2: Backend Interface and `pexpect`

- Define `TerminalBackend` and `TerminalSession`.
- Implement `PexpectTerminalBackend`.
- Route backend calls through `asyncio.to_thread`.
- Add minimal output capture.
- Add tests for fast command, long-running command, and interactive input at the backend layer.

### Milestone 3: PTY-backed `exec_command`

- Implement `exec_command` on top of the backend interface.
- Return `session_id` only for live processes.
- Add tests for fast and long-running commands.

### Milestone 4: Keep and Classify `shell_command`

- Keep `SHELL_COMMAND_TOOL` in `ALL_TOOLS`.
- Add `TOOL_PARALLEL_CONFIG`.
- Add `tool_supports_parallel`.
- Keep blocking `shell_command` writer-locked unless config allows it.
- Add tests proving `shell_command` remains available.
- Add tests for configurable parallel policy and exclusive-over-parallel precedence.

### Milestone 5: `write_stdin`

- Add stdin write and empty polling.
- Add per-session locks.
- Remove process after observed exit.
- Add interactive tests with a simple Python script.

### Milestone 6: Parallel Dispatch

- Add `aiorwlock`.
- Switch dispatch to `asyncio.gather`.
- Use reader lock for `exec_command` and `write_stdin`.
- Use `tool_supports_parallel` and `TOOL_PARALLEL_CONFIG` for `shell_command`.
- Use writer lock for `apply_patch`.

### Milestone 7: Prompt and REPL Controls

- Update system prompt.
- Add `/ps` and `/stop`.
- Ensure REPL shutdown calls `terminate_all`.

### Milestone 8: Backend Polish

- Keep `pexpect` behind the backend interface.
- Evaluate whether `ptyprocess`, raw `pty`, or an exec server is needed.
- Improve termination and process group cleanup.

### Milestone 9: Polish

- Improve output truncation with head/tail behavior.
- Add structured JSON output.
- Add lifecycle events if the UI grows beyond stdout.
- Keep `shell_command` documented alongside `exec_command`.

### Milestone 10: Write and Run the Final Test Suite

- Add unit tests for backend behavior, `shell_command` behavior, process store behavior, output buffering, cleanup, and lock policy.
- Add integration tests for parallel commands, long-running sessions, polling, stdin interaction, and `apply_patch` exclusivity.
- Run the full test suite with `python -m pytest tests`.
- Do not consider the unified exec implementation complete until these tests pass.

## Risks

- Parallel shell commands can still conflict at the filesystem level.
- Long-running background processes can keep writing after the initial tool call returns.
- PTY behavior differs between macOS, Linux, and Windows.
- Output buffering can leak memory if caps are not enforced.
- Killing process trees is platform-specific.

## Success Criteria

- Five short independent commands run concurrently and complete faster than sequential execution.
- Long-running commands return `session_id` and remain pollable.
- Fast commands do not remain in the process store.
- `shell_command` remains model-visible and tested.
- `shell_command` can be parallel-safe conditionally by config.
- tool parallel/exclusive config is covered by tests.
- `apply_patch` remains exclusive.
- All live processes are cleaned up on shutdown.
- The model understands when to use `exec_command` vs `write_stdin`.
- The final test suite covers the backend interface, the `pexpect` backend, process cleanup, parallel execution, and `apply_patch` exclusivity.

## Final Step: Write and Run Tests

End the implementation by writing and running the final test suite.

Required final work:

1. Add unit tests for the backend interface, `PexpectTerminalBackend`, `shell_command`, configurable parallel/exclusive tool policy, `ProcessManager`, output buffering, cleanup, and lock policy.
2. Add integration tests for parallel PTY commands, config-enabled parallel `shell_command`, long-running sessions, polling, interactive stdin, same-session locking, REPL shutdown cleanup, and `apply_patch` exclusivity.
3. Run:

```bash
python -m pytest tests
```

4. Fix failures before considering the full unified exec implementation complete.
