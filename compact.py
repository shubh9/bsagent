"""
Conversation history compaction.

Trigger: estimated tokens >= 90% of the model context window. Token counting
uses ceiling(byte_count/4).

Compacted history layout:
  [recent_user_msg_1, ..., recent_user_msg_n, prefixed_summary]

Recent user messages are walked backwards up to COMPACT_USER_MSG_MAX_TOKENS
(20k tokens). The summary is prefixed before being injected back into history.

Two compaction modes:
  - full: main agent session (Anthropic-inspired continuity summary)
  - sub_agent: delegated sub-task with a narrower continuation summary

Mid-turn compaction: maybe_compact() is called at the top of every agent
loop iteration, including after tool results are appended.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from openai import OpenAI

# ---- Config ------------------------------------------------------------------

APPROX_BYTES_PER_TOKEN = 4    # mirrors codex APPROX_BYTES_PER_TOKEN
COMPACT_THRESHOLD_PCT = 0.90  # 90% of context window; codex default is 95%
COMPACT_USER_MSG_MAX_TOKENS = 20_000  # mirrors COMPACT_USER_MESSAGE_MAX_TOKENS

# Context windows for known models -- mirrors context_window in models.json.
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-5.5": 272_000,
    "gpt-4.1": 1_047_576,
    "gpt-4.1-mini": 1_047_576,
    "gpt-4.1-nano": 1_047_576,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "o4-mini": 200_000,
    "o3": 200_000,
}
DEFAULT_CONTEXT_WINDOW = 128_000  # mirrors model_info_from_slug fallback

# Anthropic-inspired, OpenAI-friendly (plain prose — no XML/tag wrappers).
FULL_COMPACT_PROMPT = (
    "You have been working on the task above. Write a summary of the "
    "conversation so far.\n\n"
    "This summary will replace the earlier transcript in a future turn, "
    "where the full history may no longer be available. The goal is "
    "continuity: another model should read this summary and continue making "
    "progress without losing important context.\n\n"
    "Include anything helpful, such as:\n"
    "- What the user asked for and what success looks like\n"
    "- Current state of the work (done, in progress, blocked)\n"
    "- Important decisions, constraints, and preferences\n"
    "- Relevant file paths, commands, errors, and fixes\n"
    "- Open questions and the most sensible next steps\n\n"
    "Write in clear, structured prose. Short headings or bullet lists are "
    "fine. Do not call tools. Reply with only the summary text."
)

SUB_AGENT_COMPACT_PROMPT = (
    "You have been working on the delegated task above but have not finished "
    "it. Write a continuation summary so you (or another agent instance) can "
    "resume efficiently after the conversation history is replaced.\n\n"
    "Be structured, concise, and actionable. Cover:\n"
    "1. Task overview — core request and success criteria\n"
    "2. Current state — what is done, in progress, or blocked\n"
    "3. Important discoveries — constraints, decisions, errors encountered "
    "and fixes tried\n"
    "4. Next steps — specific actions still needed, in priority order\n"
    "5. Context to preserve — user preferences, promises made, non-obvious "
    "details\n\n"
    "Err on the side of including information that prevents duplicate work "
    "or repeated mistakes. Write in clear prose with headings or bullets as "
    "needed. Do not call tools. Reply with only the summary text."
)

FULL_SUMMARY_PREFIX = (
    "This session continues from a previous conversation that ran out of "
    "context. The summary below replaces the earlier portion of the "
    "transcript. Use it to resume the task without redoing completed work:"
)

SUB_AGENT_SUMMARY_PREFIX = (
    "This sub-agent session continues from a prior run that ran out of "
    "context. The summary below replaces earlier messages for this delegated "
    "task. Resume from this state and continue toward completion:"
)

# Backwards-compatible aliases for the main agent path.
COMPACT_PROMPT = FULL_COMPACT_PROMPT
SUMMARY_PREFIX = FULL_SUMMARY_PREFIX

_COMPACTION_PREFIXES = (FULL_SUMMARY_PREFIX, SUB_AGENT_SUMMARY_PREFIX)

# ---- Token utilities ---------------------------------------------------------


def _approx_tokens(text: str) -> int:
    """Ceiling division by APPROX_BYTES_PER_TOKEN -- mirrors approx_token_count()."""
    byte_len = len(text.encode("utf-8", errors="replace"))
    return (byte_len + APPROX_BYTES_PER_TOKEN - 1) // APPROX_BYTES_PER_TOKEN


def _estimate_history_tokens(history: list[Any]) -> int:
    return _approx_tokens(json.dumps(history, ensure_ascii=False))


def _compact_threshold(model: str) -> int:
    context_window = MODEL_CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW)
    return int(context_window * COMPACT_THRESHOLD_PCT)


# ---- Compacted history construction ------------------------------------------


def _is_summary_message(content: str) -> bool:
    """True if this message is a compaction summary -- skip when collecting."""
    return any(content.startswith(prefix) for prefix in _COMPACTION_PREFIXES)


def _collect_user_messages(history: list[Any]) -> list[str]:
    """
    Collect plain user message strings, skipping previous compaction summaries.
    Mirrors collect_user_messages() in compact.rs.
    """
    messages: list[str] = []
    for item in history:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content", "")
        if isinstance(content, str) and not _is_summary_message(content):
            messages.append(content)
    return messages


def _select_recent_user_messages(messages: list[str]) -> list[str]:
    """
    Walk backwards keeping up to COMPACT_USER_MSG_MAX_TOKENS tokens.
    Mirrors build_compacted_history_with_limit() in compact.rs.
    """
    selected: list[str] = []
    remaining = COMPACT_USER_MSG_MAX_TOKENS
    for msg in reversed(messages):
        if remaining == 0:
            break
        tokens = _approx_tokens(msg)
        if tokens <= remaining:
            selected.append(msg)
            remaining -= tokens
        else:
            selected.append(msg[: remaining * APPROX_BYTES_PER_TOKEN])
            break
    selected.reverse()
    return selected


def _build_compacted_history(
    user_messages: list[str],
    summary_text: str,
    *,
    summary_prefix: str,
) -> list[dict[str, Any]]:
    """
    Build replacement history: [selected_user_msgs..., prefixed_summary].
    """
    selected = _select_recent_user_messages(user_messages)
    compacted: list[dict[str, Any]] = [
        {"role": "user", "content": msg} for msg in selected
    ]
    compacted.append(
        {"role": "user", "content": f"{summary_prefix}\n{summary_text}"}
    )
    return compacted


def _run_compaction(
    history: list[Any],
    client: "OpenAI",
    model: str,
    *,
    compact_prompt: str,
    summary_prefix: str,
    verbose: bool,
    label: str,
) -> list[Any]:
    estimated = _estimate_history_tokens(history)
    threshold = _compact_threshold(model)

    if estimated < threshold:
        return history

    context_window = MODEL_CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW)
    if verbose:
        sys.stderr.write(
            f"\n[compacting {label} -- ~{estimated:,} estimated tokens, "
            f"threshold {threshold:,} ({int(COMPACT_THRESHOLD_PCT * 100)}% of "
            f"{context_window:,})]\n"
        )

    compaction_input = history + [{"role": "user", "content": compact_prompt}]
    resp = client.responses.create(model=model, input=compaction_input)
    summary: str = resp.output_text

    user_messages = _collect_user_messages(history)
    compacted = _build_compacted_history(
        user_messages,
        summary,
        summary_prefix=summary_prefix,
    )

    if verbose:
        sys.stderr.write(
            f"[compacted {label} {len(history)} items -> {len(compacted)} items]\n"
        )

    return compacted


# ---- Public API --------------------------------------------------------------


def maybe_compact(
    history: list[Any],
    client: "OpenAI",
    model: str,
    *,
    verbose: bool = True,
) -> list[Any]:
    """
    Compact the main agent session if history exceeds the threshold.

    Returns [recent_user_msgs..., prefixed_summary] when compacting, otherwise
    returns history unchanged.
    """
    return _run_compaction(
        history,
        client,
        model,
        compact_prompt=FULL_COMPACT_PROMPT,
        summary_prefix=FULL_SUMMARY_PREFIX,
        verbose=verbose,
        label="full",
    )


def maybe_compact_sub_agent(
    history: list[Any],
    client: "OpenAI",
    model: str,
    *,
    verbose: bool = True,
) -> list[Any]:
    """
    Compact a delegated sub-agent session if history exceeds the threshold.

    Uses a narrower continuation summary focused on resuming the sub-task.
    """
    return _run_compaction(
        history,
        client,
        model,
        compact_prompt=SUB_AGENT_COMPACT_PROMPT,
        summary_prefix=SUB_AGENT_SUMMARY_PREFIX,
        verbose=verbose,
        label="sub-agent",
    )
