from __future__ import annotations

import asyncio
import itertools
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import pexpect


@dataclass(frozen=True)
class TerminalRead:
    output: str
    exit_code: int | None
    alive: bool


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


class TerminalBackend(Protocol):
    def spawn(self, command: str, cwd: Path) -> TerminalSession:
        ...


class PexpectTerminalSession:
    def __init__(self, child: pexpect.spawn) -> None:
        self._child = child
        self._exit_code: int | None = None

    def read_until_idle_or_exit(self, timeout_s: float) -> TerminalRead:
        timeout_s = max(0.0, timeout_s)
        try:
            self._child.expect(pexpect.EOF, timeout=timeout_s)
        except pexpect.TIMEOUT:
            return TerminalRead(
                output=self._child.before or "",
                exit_code=None,
                alive=self._child.isalive(),
            )

        output = self._child.before or ""
        self._child.close()
        self._exit_code = self._child.exitstatus
        if self._exit_code is None and self._child.signalstatus is not None:
            self._exit_code = 128 + self._child.signalstatus
        return TerminalRead(output=output, exit_code=self._exit_code, alive=False)

    def write(self, data: str) -> None:
        if not self._child.isalive():
            return
        self._child.send(data)

    def is_alive(self) -> bool:
        return self._child.isalive()

    def exit_code(self) -> int | None:
        return self._exit_code

    def terminate(self) -> None:
        if self._child.isalive():
            self._child.terminate(force=True)
        self._child.close()
        self._exit_code = self._child.exitstatus


class PexpectTerminalBackend:
    def spawn(self, command: str, cwd: Path) -> TerminalSession:
        return PexpectTerminalSession(
            pexpect.spawn(
                "/bin/bash",
                ["-lc", command],
                cwd=str(cwd),
                encoding="utf-8",
                echo=False,
                env=os.environ.copy(),
            )
        )


@dataclass
class ManagedTerminal:
    session_id: int
    command: str
    cwd: Path
    session: TerminalSession
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used_at: float = field(default_factory=time.monotonic)


class ProcessManager:
    def __init__(self, backend: TerminalBackend | None = None) -> None:
        self._backend = backend or PexpectTerminalBackend()
        self._lock = asyncio.Lock()
        self._next_id = itertools.count(1001)
        self._sessions: dict[int, ManagedTerminal] = {}

    async def allocate_id(self) -> int:
        async with self._lock:
            return next(self._next_id)

    async def spawn(self, session_id: int, command: str, cwd: Path) -> ManagedTerminal:
        session = await asyncio.to_thread(self._backend.spawn, command, cwd)
        return ManagedTerminal(
            session_id=session_id,
            command=command,
            cwd=cwd,
            session=session,
        )

    async def store(self, managed: ManagedTerminal) -> None:
        async with self._lock:
            managed.last_used_at = time.monotonic()
            self._sessions[managed.session_id] = managed

    async def get(self, session_id: int) -> ManagedTerminal | None:
        async with self._lock:
            managed = self._sessions.get(session_id)
            if managed is not None:
                managed.last_used_at = time.monotonic()
            return managed

    async def remove(self, session_id: int) -> ManagedTerminal | None:
        async with self._lock:
            return self._sessions.pop(session_id, None)

    async def terminate_all(self) -> None:
        async with self._lock:
            managed_sessions = list(self._sessions.values())
            self._sessions.clear()
        await asyncio.gather(
            *(
                asyncio.to_thread(managed.session.terminate)
                for managed in managed_sessions
            ),
            return_exceptions=True,
        )

    async def session_count(self) -> int:
        async with self._lock:
            return len(self._sessions)


process_manager = ProcessManager()
