# Full Unified Exec Remaining Work

The simple parallel slice is implemented. `bsagent` now has `shell_command`, `exec_command`, `write_stdin`, `apply_patch`, a PTY backend interface, `pexpect`, a basic process manager, configurable tool parallelism, per-session locks, and tests for the simple path.

This document is only the remaining work needed to turn that simple slice into the full unified exec runtime.

## 1. Continuous Background Output Reading

Replace the current "read only during tool calls" behavior with managed background readers.

- Add a `ManagedProcess`/`ManagedTerminal` lifecycle that starts reader and exit watcher tasks when a process is stored.
- Continuously read PTY output in the background and append it to an output buffer.
- Track process exit status as soon as the process exits, even if the model never polls again.
- Make `write_stdin(chars="")` read from buffered output instead of directly blocking on `pexpect.expect(...)`.
- Add a way for poll calls to wait until new output, process exit, or timeout.
- Ensure reader tasks cleanly stop and remove finished sessions from the process store.

## 2. Output Buffer

Add bounded output storage so long-running commands cannot grow memory forever.

- Implement `OutputBuffer`.
- Cap stored output with `OUTPUT_MAX_BYTES`.
- Return snapshots capped by `max_output_tokens` / `OUTPUT_CAP`.
- Track whether output was truncated.
- Prefer head/tail truncation over only keeping the latest output.
- Include metadata such as `original_token_count` or `original_byte_count` when useful.

## 3. Process Store Hardening

Make the process manager robust enough for dev servers, watchers, and forgotten sessions.

- Add `MAX_PROCESSES`.
- Add pruning:
  - remove exited sessions first
  - preserve recently used live sessions
  - terminate least-recently-used live sessions if over cap
- Add `terminate(session_id)`.
- Improve `terminate_all()`.
- Handle spawn failures after ID allocation.
- Track `started_at`, `last_used_at`, command, cwd, exit code, and alive/exited state.
- Avoid leaking sessions if a tool call is cancelled.

## 4. Stop And Inspect Controls

Add user-facing controls for live background sessions.

- Add `/ps` in the REPL to list live sessions.
- Add `/stop` to terminate all live sessions.
- Add `/stop <session_id>` to terminate one session.
- Show command, cwd, age, last-used time, exit status, and output summary in `/ps`.
- Ensure one-shot and REPL shutdown still call `terminate_all()`.

## 5. `write_stdin` Semantics

Tighten the polling/interactivity contract.

- Empty `chars` means poll buffered output.
- Non-empty `chars` writes to stdin, then waits for new output/exit/timeout.
- Same-session calls stay serialized by the per-session lock.
- Different sessions remain parallel.
- Unknown `session_id` remains a model-correctable error.
- Exited sessions should return final buffered output and exit code, then be removed.
- Decide whether completed sessions are removed immediately or retained briefly for a final read.

## 6. `shell_command` Policy

Keep `shell_command` first-class.

- Keep the current simple `subprocess.run` implementation unless there is a clear reason to route it through the managed PTY backend.
- Keep its parallel behavior controlled only by `TOOL_PARALLEL_CONFIG`.
- Preserve the rule that `exclusive_tools` wins over `parallel_tools`.
- Keep tests proving `shell_command` is visible, works, and can be made parallel-safe by config.

## 7. PTY Backend Polish

Keep `pexpect` behind the backend interface, but improve operational behavior.

- Add process group cleanup where possible.
- Normalize exit codes for signal termination.
- Reduce shell startup noise if possible, for example by avoiding login/profile startup behavior when safe.
- Keep backend-specific APIs out of `tools.py`.
- Re-evaluate `ptyprocess`, raw `pty`, or an exec server only if `pexpect` blocks the full behavior.

## 8. Tool Response Shape

Standardize JSON results across `exec_command` and `write_stdin`.

Responses should consistently include:

- `output`
- `exit_code`
- `wall_time_seconds`
- `session_id` only when still running
- truncation metadata when output was capped
- optional process metadata such as command/cwd when helpful

Fast completed commands must not return `session_id`. Running commands must return `session_id`.

## 9. Prompt And Logging Polish

Make tool usage easier to understand from logs.

- Keep the model prompt clear about:
  - `shell_command` for simple one-shot commands
  - `exec_command` for long-running or interactive commands
  - `write_stdin(chars="")` for polling
  - `write_stdin(chars="...\n")` for prompts
  - avoiding conflicting writes in parallel
- Make verbose logs explicitly show tool names, e.g. `tool=exec_command`, `tool=shell_command`, `tool=write_stdin`.
- Consider showing `session_id` in the `exec_command` preview when a process remains alive.

## 10. Tests To Add Or Keep

The full implementation is not done until these tests pass.

Unit tests:

- `OutputBuffer` caps output and reports truncation.
- `OutputBuffer` preserves useful head/tail output.
- `ProcessManager` stores, gets, removes, terminates, and prunes sessions.
- Background reader appends output without an explicit poll.
- Exit watcher records exit code and removes or marks sessions.
- Unknown `session_id` returns a model-correctable error.
- Same-session `write_stdin` calls are serialized.
- Different sessions can be polled concurrently.
- `shell_command` remains model-visible and config-controlled.
- `apply_patch` remains exclusive.

Integration tests:

- Several independent `exec_command` calls run concurrently.
- Long-running command returns `session_id`.
- Background output is available on a later poll.
- Interactive stdin works.
- Dev-server-like process can be stopped.
- `/ps`, `/stop`, and `/stop <session_id>` work.
- REPL shutdown terminates live processes.
- Large output is truncated without memory growth.
- Config-enabled parallel `shell_command` works.
- Full test suite passes with `python -m pytest tests`.

Manual smoke tests:

```bash
python -m pytest tests
bsagent "run five independent sleep/echo commands in parallel"
bsagent "start a long-running python loop, poll it twice, then stop it"
bsagent "start an interactive python input prompt and answer it"
```

## Completion Criteria

- Commands can keep running after `exec_command` returns.
- Output is captured continuously in the background.
- Polling returns buffered output and does not depend on directly reading the PTY only during the poll call.
- All live processes are visible, stoppable, and cleaned up.
- Output is bounded and truncation is explicit.
- `shell_command` stays available and config-controlled.
- `apply_patch` stays exclusive.
- Tests cover process lifecycle, output buffering, locking, cleanup, and manual REPL controls.
