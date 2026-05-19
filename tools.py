"""
Tool definitions, dispatch, and the asyncio.Lock execution gate.

Mirrors codex-rs tools/parallel.rs: both shell_command and apply_patch
hold a single write-lock (_shell_lock) so they always run sequentially,
even when the model batches multiple calls in one response.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any


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

# Both tools acquire this lock before doing any I/O.
# Sequential by design — mirrors codex RwLock write-lock for shell/patch ops.
_shell_lock = asyncio.Lock()

# ─── Tool schemas (exact codex names and parameter shapes) ───────────────────

SHELL_COMMAND_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "shell_command",
    "description": (
        "Run a shell command in the working directory and return combined "
        "stdout + stderr. Use for reading files, running tests, searching "
        "code, installing packages, or any other shell operation."
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

ALL_TOOLS = [SHELL_COMMAND_TOOL, APPLY_PATCH_TOOL]

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
                content_lines.append(lines[i])
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
    Execute tool calls sequentially under the shell lock.

    Both shell_command and apply_patch hold _shell_lock exclusively —
    mirrors the codex RwLock write-lock pattern: side-effectful ops
    never run in parallel.

    ToolCallError.respond_to_model is caught here and returned as a
    function_call_output so the model can see and correct its mistake.
    Any other exception (including ToolCallError with fatal=True) propagates
    and crashes the process — do not swallow unexpected failures.
    """
    results: list[dict[str, Any]] = []
    for tc in tool_calls:
        call_id: str = tc.get("call_id", tc.get("id", ""))
        async with _shell_lock:
            try:
                result = await _run_one(tc, workdir)
            except ToolCallError as exc:
                if exc.fatal:
                    raise
                result = {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": f"Error: {exc}",
                }
        results.append(result)
    return results


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
        output = _run_shell_command(command, cwd, timeout)

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
