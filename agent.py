"""
bsagent — codex-style local CLI coding agent.

Entry point for the `bsagent` launcher script.
The agent loop, tools, and compaction live in loop.py / tools.py / compact.py.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from openai import OpenAI

from loop import continue_agent, run_agent
from unified_exec import process_manager


# ─── Bootstrap ────────────────────────────────────────────────────────────────


def _load_dotenv() -> None:
    """Minimal .env loader — no extra dependency required."""
    candidates = [Path(__file__).parent / ".env", Path.cwd() / ".env"]
    seen: set[Path] = set()
    for env_path in candidates:
        env_path = env_path.resolve()
        if env_path in seen or not env_path.is_file():
            continue
        seen.add(env_path)
        for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


_load_dotenv()

WORKDIR = Path(os.environ.get("AGENT_WORKDIR", os.getcwd())).resolve()
MODEL = os.environ.get("AGENT_MODEL", "gpt-5.5")


# ─── REPL ─────────────────────────────────────────────────────────────────────


async def repl(client: OpenAI) -> None:
    print(f"bsagent ready  model={MODEL}  workdir={WORKDIR}")
    print("type a request, or 'exit' to quit.\n")

    history: list = []
    try:
        while True:
            try:
                user_input = input(">>> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit"}:
                break

            print()
            final, history = await continue_agent(
                user_input,
                history,
                client=client,
                model=MODEL,
                workdir=WORKDIR,
                verbose=True,
            )
            # Print final message to stdout (tool output goes to stderr during the run)
            if final:
                print(final)
            print()
    finally:
        await process_manager.terminate_all()


# ─── One-shot ─────────────────────────────────────────────────────────────────


async def one_shot(prompt: str, client: OpenAI) -> None:
    """Run a single prompt non-interactively and print the final message."""
    print(f"bsagent  model={MODEL}  workdir={WORKDIR}")
    print(f">>> {prompt}\n")
    try:
        result = await run_agent(
            prompt,
            client=client,
            model=MODEL,
            workdir=WORKDIR,
            verbose=True,
        )
        if result:
            print(result)
    finally:
        await process_manager.terminate_all()


# ─── Entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: set OPENAI_API_KEY in your environment first.", file=sys.stderr)
        sys.exit(1)

    client = OpenAI()

    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
        asyncio.run(one_shot(prompt, client))
    else:
        asyncio.run(repl(client))


if __name__ == "__main__":
    main()
