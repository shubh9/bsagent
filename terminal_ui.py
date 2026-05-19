"""
Terminal session UI for bsagent.

  print_session_summary()  — static Rich table shown after each agent turn
  open_terminal_viewer()   — interactive Textual TUI opened via /terminals
"""
from __future__ import annotations

import re
import time

from rich.console import Console
from rich.table import Table
from rich.text import Text

from unified_exec import SessionInfo, process_manager

_TAIL_CHARS = 6000
_MAX_LINES = 50
_console = Console(stderr=True, highlight=False)

# Strip cursor-movement CSI sequences; preserve SGR colour codes.
_CSI_RE = re.compile(r'\x1b\[[\x30-\x3f]*[\x20-\x2f]*([@-~])')


def _clean_ansi(text: str) -> str:
    def _sub(m: re.Match[str]) -> str:
        return m.group(0) if m.group(1) == "m" else ""
    return _CSI_RE.sub(_sub, text)


# ─── Static summary (shown once after each agent turn) ────────────────────────


async def print_session_summary() -> None:
    """Print a compact one-line-per-session table to stderr if any sessions are alive."""
    snapshots = await process_manager.get_session_snapshots(0)
    alive = [info for info, _ in snapshots if info.alive]
    if not alive:
        return

    table = Table(
        show_header=True,
        header_style="bold dim",
        border_style="dim blue",
        pad_edge=True,
    )
    table.add_column("ID", style="bold cyan", width=6, no_wrap=True)
    table.add_column("Command", no_wrap=True, max_width=55)
    table.add_column("Status", width=10)
    table.add_column("Age", width=5, justify="right")

    for info in alive:
        age = f"{int(time.monotonic() - info.started_at)}s"
        table.add_row(
            f"#{info.session_id}",
            info.command[:55],
            Text("running", style="green"),
            age,
        )

    _console.print()
    _console.print(table)
    _console.print("[dim]  type /terminals to inspect live output[/dim]")
    _console.print()


# ─── Interactive Textual viewer (/terminals) ──────────────────────────────────


async def open_terminal_viewer() -> None:
    """Open an interactive Textual TUI for navigating and viewing terminal sessions."""
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Horizontal, ScrollableContainer, Vertical
        from textual.widgets import Footer, Header, Label, ListItem, ListView, Static
    except ImportError:
        _console.print("[red]textual not installed — run: .venv/bin/pip install textual[/red]")
        return

    snapshots = await process_manager.get_session_snapshots(_TAIL_CHARS)
    if not snapshots:
        _console.print("[dim]No active terminal sessions.[/dim]")
        return

    class TerminalViewer(App[None]):
        TITLE = "Terminal Sessions"
        BINDINGS = [
            Binding("q", "quit", "Quit", show=True),
            Binding("r", "refresh_list", "Refresh list", show=True),
        ]
        CSS = """
        Screen {
            layout: horizontal;
        }
        #sidebar {
            width: 36;
            height: 100%;
            border-right: solid $accent-darken-2;
            background: $surface;
        }
        ListView {
            height: 100%;
            background: $surface;
        }
        ListView > ListItem {
            padding: 0 1;
        }
        ListView > ListItem.--highlight {
            background: $accent-darken-2;
        }
        #content {
            width: 1fr;
            height: 100%;
            layout: vertical;
        }
        #session-title {
            height: 1;
            background: $boost;
            padding: 0 1;
            color: $text;
            text-style: bold;
        }
        #output-scroll {
            width: 100%;
            height: 1fr;
            padding: 0 1;
        }
        """

        def __init__(self, initial: list[tuple[SessionInfo, str]]) -> None:
            super().__init__()
            self._snapshots = list(initial)
            self._sel = 0

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            with Horizontal():
                with Vertical(id="sidebar"):
                    yield ListView(
                        *self._make_list_items(),
                        id="session-list",
                    )
                with Vertical(id="content"):
                    yield Static("", id="session-title")
                    with ScrollableContainer(id="output-scroll"):
                        yield Static("", id="output", markup=False)
            yield Footer()

        def _make_list_items(self) -> list[ListItem]:
            items = []
            for info, _ in self._snapshots:
                dot = "●" if info.alive else "○"
                color = "green" if info.alive else "dim"
                cmd = info.command[:27]
                items.append(
                    ListItem(Label(f"[{color}]{dot}[/{color}] #{info.session_id}  {cmd}"))
                )
            return items

        def on_mount(self) -> None:
            self._draw()
            self.set_interval(0.5, self._tick)

        def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
            all_items = list(self.query(ListItem))
            if event.item is not None and event.item in all_items:
                self._sel = all_items.index(event.item)
                self._draw()

        async def _tick(self) -> None:
            live = await process_manager.get_session_snapshots(_TAIL_CHARS)
            snap_map = {info.session_id: (info, out) for info, out in live}
            # Update output/status for existing sessions; keep order
            self._snapshots = [
                snap_map.get(info.session_id, (info, prev_out))
                for info, prev_out in self._snapshots
            ]
            # Refresh sidebar dots
            all_items = list(self.query(ListItem))
            for i, (info, _) in enumerate(self._snapshots):
                if i >= len(all_items):
                    break
                dot = "●" if info.alive else "○"
                color = "green" if info.alive else "dim"
                cmd = info.command[:27]
                all_items[i].query_one(Label).update(
                    f"[{color}]{dot}[/{color}] #{info.session_id}  {cmd}"
                )
            self._draw()

        async def action_refresh_list(self) -> None:
            """Re-query sessions and rebuild the sidebar list."""
            live = await process_manager.get_session_snapshots(_TAIL_CHARS)
            self._snapshots = live
            self._sel = min(self._sel, max(0, len(live) - 1))
            lv = self.query_one(ListView)
            await lv.clear()
            for item in self._make_list_items():
                await lv.append(item)
            self._draw()

        def _draw(self) -> None:
            if not self._snapshots:
                self.query_one("#session-title", Static).update("No active sessions")
                self.query_one("#output", Static).update("")
                return

            idx = min(self._sel, len(self._snapshots) - 1)
            info, output = self._snapshots[idx]

            age = int(time.monotonic() - info.started_at)
            if info.alive:
                status = "[green]● running[/green]"
            elif info.exit_code == 0:
                status = "[green]✓ done[/green]"
            else:
                status = f"[red]✗ exit={info.exit_code}[/red]"

            title = (
                f"[bold cyan]#{info.session_id}[/bold cyan]"
                f"  {info.command[:60]}"
                f"  {status}"
                f"  [dim]{age}s[/dim]"
            )
            self.query_one("#session-title", Static).update(title)

            cleaned = _clean_ansi(output)
            tail = "\n".join(cleaned.splitlines()[-_MAX_LINES:])
            try:
                body = Text.from_ansi(tail)
            except Exception:
                body = Text(tail)
            self.query_one("#output", Static).update(body)

    app = TerminalViewer(snapshots)
    await app.run_async()
