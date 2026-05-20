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
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML

from loop import continue_agent, run_agent
from mcp_bridge import McpManager, default_mcp_server_specs
from terminal_ui import open_terminal_viewer, print_session_summary
from unified_exec import SessionInfo, process_manager

# ─── Terminal count tracker ───────────────────────────────────────────────────

_active_terminal_count = 0


async def _poll_terminal_count() -> None:
    """Background task: refreshes active terminal count every second."""
    global _active_terminal_count
    while True:
        try:
            sessions = await process_manager.list_sessions()
            _active_terminal_count = sum(1 for s in sessions if s.alive)
        except Exception:
            pass
        await asyncio.sleep(1)


def _terminal_toolbar() -> HTML | str:
    n = _active_terminal_count
    if n == 0:
        return ""
    plural = "s" if n != 1 else ""
    return HTML(f"<b>{n}</b> terminal{plural} running · /terminals to inspect")


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


def _format_session(info: SessionInfo) -> str:
    age = int(max(0, time_now() - info.started_at))
    status = "running" if info.alive else f"exit={info.exit_code}"
    return (
        f"{info.session_id}  {status}  age={age}s  "
        f"cwd={info.cwd}  output={info.output_chars} chars  cmd={info.command}"
    )


def time_now() -> float:
    import time

    return time.monotonic()


async def _handle_repl_command(user_input: str) -> bool:
    if user_input == "/terminals":
        await open_terminal_viewer()
        return True

    if user_input == "/ps":
        sessions = await process_manager.list_sessions()
        if not sessions:
            print("no background sessions")
        else:
            for session in sessions:
                print(_format_session(session))
        return True

    if user_input == "/stop":
        await process_manager.terminate_all()
        print("stopped all background sessions")
        return True

    if user_input.startswith("/stop "):
        raw_session_id = user_input.removeprefix("/stop ").strip()
        try:
            session_id = int(raw_session_id)
        except ValueError:
            print(f"invalid session id: {raw_session_id}")
            return True
        stopped = await process_manager.terminate(session_id)
        print(f"stopped session {session_id}" if stopped else f"unknown session {session_id}")
        return True

    return False


async def repl(client: OpenAI) -> None:
    print(f"bsagent ready  model={MODEL}  workdir={WORKDIR}")
    print("type a request, '/terminals', '/ps', '/stop', or 'exit' to quit.\n")

    history: list = []
    mcp_manager = McpManager(default_mcp_server_specs(WORKDIR))
    prompt_session: PromptSession = PromptSession()
    count_task = asyncio.create_task(_poll_terminal_count())
    try:
        for warning in await mcp_manager.start(WORKDIR):
            print(f"warning: {warning}", file=sys.stderr)

        while True:
            try:
                user_input = (
                    await prompt_session.prompt_async(">>> ", bottom_toolbar=_terminal_toolbar)
                ).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit"}:
                break
            if await _handle_repl_command(user_input):
                print()
                continue

            print()
            final, history = await continue_agent(
                user_input,
                history,
                client=client,
                model=MODEL,
                workdir=WORKDIR,
                verbose=True,
                mcp_manager=mcp_manager,
            )
            await print_session_summary()
            print()
    finally:
        count_task.cancel()
        await mcp_manager.aclose()
        await process_manager.terminate_all()


# ─── One-shot ─────────────────────────────────────────────────────────────────


async def one_shot(prompt: str, client: OpenAI) -> None:
    """Run a single prompt non-interactively and print the final message."""
    print(f"bsagent  model={MODEL}  workdir={WORKDIR}")
    print(f">>> {prompt}\n")
    mcp_manager = McpManager(default_mcp_server_specs(WORKDIR))
    try:
        for warning in await mcp_manager.start(WORKDIR):
            print(f"warning: {warning}", file=sys.stderr)

        result = await run_agent(
            prompt,
            client=client,
            model=MODEL,
            workdir=WORKDIR,
            verbose=True,
            mcp_manager=mcp_manager,
        )
        await print_session_summary()
    finally:
        await mcp_manager.aclose()
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
