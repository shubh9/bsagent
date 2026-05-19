"""
Core agent loop.

Mirrors the codex-rs session/turn.rs run_turn() pattern:
  1. Compact history if approaching context limit
  2. Stream one turn from the model
  3. If tool calls returned → execute → append results → repeat
  4. If no tool calls → return final message text
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openai import OpenAI

from compact import maybe_compact
from tools import ALL_TOOLS, ToolCallError, dispatch_tools

if TYPE_CHECKING:
    from mcp_bridge import McpManager

# ─── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a local CLI coding assistant with shell access.

Use shell_command to read files, run tests, search code, and execute \
simple one-shot commands. Use exec_command for long-running, interactive, \
or pollable commands. Use write_stdin with empty chars to poll an \
exec_command session, or with text ending in \\n to answer prompts. Use \
apply_patch for structured file edits — it is safer than shell redirection \
and produces clean diffs.

When browser MCP tools are available, use exec_command to start local dev \
servers, read the localhost URL from their output, then use the browser tools \
to navigate, inspect the page, interact with elements, take screenshots, and \
record browser videos/traces when useful.

Guidelines:
- Explore the codebase before making changes (ls, cat, grep).
- Prefer apply_patch over write-via-shell for editing existing files.
- Batch independent exec_command calls in the same assistant response when useful.
- Use shell_command for quick one-shot commands unless the command is interactive,
  long-running, or needs a session_id.
- Avoid running conflicting file-writing commands in parallel.
- If a command fails, read the error and try a different approach.
- When the task is complete, summarise what you did concisely.\
"""

# ─── Streaming turn ───────────────────────────────────────────────────────────


async def _stream_turn(
    history: list[Any],
    client: OpenAI,
    model: str,
    *,
    verbose: bool,
    mcp_manager: "McpManager | None" = None,
) -> tuple[list[dict[str, Any]], str, list[Any]]:
    """
    Make one streaming model call.

    Returns (tool_calls, final_text, assistant_output_items) where:
      - tool_calls  — list of raw function_call dicts from response.output
      - final_text  — the agent's final text (empty string if tool calls present)
      - assistant_output_items — the full response.output list for history append
    """
    tool_calls: list[dict[str, Any]] = []
    text_parts: list[str] = []
    output_items: list[Any] = []


    available_tools = ALL_TOOLS + (mcp_manager.openai_tools() if mcp_manager else [])

    with client.responses.stream(
        model=model,
        instructions=SYSTEM_PROMPT,
        input=history,
        tools=available_tools,
        parallel_tool_calls=True,
    ) as stream:
        for event in stream:
            event_type = getattr(event, "type", None)
            if event_type == "response.output_text.delta":
                delta = getattr(event, "delta", "")
                text_parts.append(delta)
                if verbose:
                    sys.stderr.write(delta)
                    sys.stderr.flush()

        response = stream.get_final_response()

    if verbose and text_parts:
        sys.stderr.write("\n")

    # Separate tool calls from text output
    for item in response.output:
        item_type = getattr(item, "type", None)
        if item_type == "function_call":
            tc = {
                "name": getattr(item, "name", ""),
                "call_id": getattr(item, "call_id", getattr(item, "id", "")),
                "arguments": getattr(item, "arguments", "{}"),
            }
            tool_calls.append(tc)
            if verbose:
                _print_tool_call(tc)
        output_items.append(item)

    # Serialize output items to plain dicts for history re-use.
    # Use an allowlist of fields the Responses API actually accepts as input —
    # models may return extra fields (status, parsed_arguments, annotations…)
    # that are valid on output but rejected when passed back as input.
    serialized_output = [_clean_for_input(item) for item in output_items]

    final_text = "".join(text_parts)
    return tool_calls, final_text, serialized_output


def _clean_for_input(item: Any) -> dict[str, Any]:
    """
    Strip fields that the Responses API rejects when output items are passed
    back as input. Uses an allowlist per item type — safer than a blocklist
    because models may return new fields at any time.
    """
    d = item.model_dump() if hasattr(item, "model_dump") else dict(item)
    item_type = d.get("type", "")

    if item_type == "function_call":
        return {k: d[k] for k in ("type", "id", "call_id", "name", "arguments") if k in d}

    if item_type == "message":
        cleaned: dict[str, Any] = {k: d[k] for k in ("type", "id", "role") if k in d}
        if "content" in d and isinstance(d["content"], list):
            cleaned["content"] = [
                {ck: cv for ck, cv in c.items() if ck in ("type", "text", "refusal")}
                for c in d["content"]
            ]
        return cleaned

    if item_type == "reasoning":
        # Reasoning items must be echoed back as-is so the model can reference them,
        # but the Responses API rejects "status" on input — strip it.
        return {k: v for k, v in d.items() if k != "status"}

    # Unknown type: fail hard. If a future model returns a new item type we
    # don't know how to clean, silently passing it through would produce a
    # cryptic API error on the next turn. A loud crash here points directly
    # at the problem. Add an explicit branch above when a new type is needed.
    raise ValueError(
        f"_clean_for_input: unhandled response item type {item_type!r}. "
        "Add an explicit allowlist branch for this type."
    )


def _print_tool_call(tc: dict[str, Any]) -> None:
    name = tc["name"]
    args = json.loads(tc["arguments"])

    if name == "shell_command":
        cmd = args.get("command", "")
        wd = f" (in {args['workdir']})" if "workdir" in args else ""
        sys.stderr.write(f"\n\033[35mtool=shell_command $\033[0m \033[1m{cmd}\033[0m{wd}\n")
    elif name == "exec_command":
        cmd = args.get("cmd", "")
        wd = f" (in {args['workdir']})" if "workdir" in args else ""
        sys.stderr.write(f"\n\033[35mtool=exec_command pty$\033[0m \033[1m{cmd}\033[0m{wd}\n")
    elif name == "write_stdin":
        session_id = args.get("session_id", "")
        chars = args.get("chars", "")
        action = "poll" if chars == "" else "write"
        sys.stderr.write(f"\n\033[35mtool=write_stdin\033[0m {action} session={session_id}\n")
    elif name == "apply_patch":
        patch = args.get("patch", "")
        preview = next(
            (l for l in patch.splitlines() if l.startswith("*** ") and "File:" in l),
            "apply_patch",
        )
        sys.stderr.write(f"\n\033[36mtool=apply_patch\033[0m {preview.replace('*** ', '')}\n")
    elif name.startswith("mcp__"):
        sys.stderr.write(f"\n\033[34mtool={name}\033[0m\n")
    else:
        sys.stderr.write(f"\n\033[33mtool\033[0m {name}\n")


# ─── Agent loop ───────────────────────────────────────────────────────────────


async def run_agent(
    prompt: str,
    *,
    client: OpenAI,
    model: str,
    workdir: Path,
    verbose: bool = True,
    mcp_manager: "McpManager | None" = None,
) -> str:
    """
    Run the agent loop for a single user prompt.

    Mirrors codex session/turn.rs:
      - Compact → model call → tool dispatch → repeat until no tool calls
    Returns the final agent message text.
    """
    history: list[Any] = [{"role": "user", "content": prompt}]

    while True:
        # ① Compact if history is approaching context limit
        history = maybe_compact(history, client, model, verbose=verbose)

        # ② Stream one model turn
        tool_calls, final_text, output_items = await _stream_turn(
            history, client, model, verbose=verbose, mcp_manager=mcp_manager
        )

        # Extend history with assistant output items flat (Responses API format:
        # function_call items are top-level, not nested in a content array)
        history.extend(output_items)

        # ③ No tool calls → agent is done
        if not tool_calls:
            return final_text

        # ④ Execute tools (sequentially under asyncio.Lock)
        results = await dispatch_tools(tool_calls, workdir, mcp_manager=mcp_manager)

        for r in results:
            preview = r["output"].splitlines()[0][:100] if r["output"] else ""
            if verbose:
                sys.stderr.write(f"\033[2m  → {preview}\033[0m\n")

        # Tool results also go as flat top-level items
        history.extend(results)


async def continue_agent(
    user_message: str,
    history: list[Any],
    *,
    client: OpenAI,
    model: str,
    workdir: Path,
    verbose: bool = True,
    mcp_manager: "McpManager | None" = None,
) -> tuple[str, list[Any]]:
    """
    Continue an existing conversation with a new user message.

    Used by the REPL to preserve history across turns.
    Returns (final_text, updated_history).
    """
    history = history + [{"role": "user", "content": user_message}]

    while True:
        history = maybe_compact(history, client, model, verbose=verbose)

        tool_calls, final_text, output_items = await _stream_turn(
            history, client, model, verbose=verbose, mcp_manager=mcp_manager
        )

        history.extend(output_items)

        if not tool_calls:
            return final_text, history

        results = await dispatch_tools(tool_calls, workdir, mcp_manager=mcp_manager)

        for r in results:
            preview = r["output"].splitlines()[0][:100] if r["output"] else ""
            if verbose:
                sys.stderr.write(f"\033[2m  → {preview}\033[0m\n")

        history.extend(results)
