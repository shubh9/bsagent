"""
Conversation history compaction.

Mirrors codex-rs compact.rs: when the estimated token count of the
history approaches the model's context window, a second model call
produces a handoff summary that replaces the full history.
"""

from __future__ import annotations

import sys
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from openai import OpenAI

# ─── Config ───────────────────────────────────────────────────────────────────

# Estimated token threshold before compacting.
# Rough heuristic: 1 token ≈ 4 chars. 60k tokens gives headroom
# for the model's response before hitting a typical 128k context window.
COMPACT_THRESHOLD_TOKENS = 60_000

# Mirrors codex templates/compact/prompt.md
COMPACT_PROMPT = """\
You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff \
summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences discovered
- What remains to be done (clear next steps)
- Any critical data, file contents, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly \
continue the work without losing important context.\
"""

# ─── Implementation ───────────────────────────────────────────────────────────


def _estimate_tokens(history: list[Any]) -> int:
    """Rough token estimate: 1 token ≈ 4 chars of JSON."""
    return sum(len(str(item)) for item in history) // 4


def maybe_compact(
    history: list[Any],
    client: "OpenAI",
    model: str,
    *,
    verbose: bool = True,
) -> list[Any]:
    """
    Return history unchanged if under the token threshold.

    If over the threshold, call the model to produce a handoff summary,
    then return a replacement history of the form:

        [summary_as_user_msg, ack_as_assistant_msg, last_user_msg]

    This mirrors codex InitialContextInjection::DoNotInject — the next
    regular turn will re-inject system context fresh.
    """
    estimated = _estimate_tokens(history)
    if estimated < COMPACT_THRESHOLD_TOKENS:
        return history

    if verbose:
        sys.stderr.write(
            f"\n[compacting — ~{estimated:,} tokens, threshold {COMPACT_THRESHOLD_TOKENS:,}]\n"
        )

    compaction_input = history + [{"role": "user", "content": COMPACT_PROMPT}]

    resp = client.responses.create(
        model=model,
        input=compaction_input,
    )
    summary: str = resp.output_text

    # Find the last user message to preserve as the active task context
    last_user = next(
        (m for m in reversed(history) if isinstance(m, dict) and m.get("role") == "user"),
        None,
    )

    compacted: list[Any] = [
        {"role": "user", "content": summary},
        {"role": "assistant", "content": "Understood. I'll continue from where we left off."},
    ]
    if last_user is not None:
        compacted.append(last_user)

    if verbose:
        sys.stderr.write(
            f"[compacted {len(history)} items → {len(compacted)} items]\n"
        )

    return compacted
