from __future__ import annotations

import asyncio
import itertools
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import pexpect


OUTPUT_MAX_CHARS = 1024 * 1024
MAX_PROCESSES = 64
READ_CHUNK_SIZE = 4096
READ_IDLE_TIMEOUT_S = 0.05
YIELD_AFTER_IDLE_S = 0.25


@dataclass(frozen=True)
class TerminalRead:
    output: str
    exit_code: int | None
    alive: bool


@dataclass(frozen=True)
class OutputSnapshot:
    output: str
    next_offset: int
    original_char_count: int
    truncated: bool


@dataclass(frozen=True)
class SessionInfo:
    session_id: int
    command: str
    cwd: Path
    started_at: float
    last_used_at: float
    exit_code: int | None
    alive: bool
    output_chars: int
    truncated: bool


class TerminalSession(Protocol):
    def read_chunk(self, timeout_s: float) -> TerminalRead:
        ...

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

    def read_chunk(self, timeout_s: float) -> TerminalRead:
        try:
            output = self._child.read_nonblocking(
                size=READ_CHUNK_SIZE,
                timeout=max(0.0, timeout_s),
            )
            return TerminalRead(output=output or "", exit_code=None, alive=True)
        except pexpect.TIMEOUT:
            return TerminalRead(output="", exit_code=None, alive=self._child.isalive())
        except pexpect.EOF:
            self._child.close()
            self._exit_code = self._normalize_exit_code()
            return TerminalRead(output=self._child.before or "", exit_code=self._exit_code, alive=False)

    def read_until_idle_or_exit(self, timeout_s: float) -> TerminalRead:
        deadline = time.monotonic() + max(0.0, timeout_s)
        chunks: list[str] = []

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return TerminalRead(
                    output="".join(chunks),
                    exit_code=None,
                    alive=self._child.isalive(),
                )

            read = self.read_chunk(remaining)
            if read.output:
                chunks.append(read.output)
            if not read.alive:
                return TerminalRead(
                    output="".join(chunks),
                    exit_code=read.exit_code,
                    alive=False,
                )
            if not read.output:
                return TerminalRead(output="".join(chunks), exit_code=None, alive=True)

    def write(self, data: str) -> None:
        if self._child.isalive():
            self._child.send(data)

    def is_alive(self) -> bool:
        try:
            return self._child.isalive()
        except pexpect.ExceptionPexpect:
            return False

    def exit_code(self) -> int | None:
        if self._exit_code is not None:
            return self._exit_code
        if not self._child.isalive():
            self._child.close()
            self._exit_code = self._normalize_exit_code()
        return self._exit_code

    def terminate(self) -> None:
        try:
            if self._child.isalive():
                self._child.terminate(force=True)
        except pexpect.ExceptionPexpect:
            pass
        try:
            self._child.close()
        except pexpect.ExceptionPexpect:
            pass
        self._exit_code = self._normalize_exit_code()

    def _normalize_exit_code(self) -> int | None:
        if self._child.exitstatus is not None:
            return self._child.exitstatus
        if self._child.signalstatus is not None:
            return 128 + self._child.signalstatus
        return None


class PexpectTerminalBackend:
    def spawn(self, command: str, cwd: Path) -> TerminalSession:
        return PexpectTerminalSession(
            pexpect.spawn(
                "/bin/bash",
                ["--noprofile", "--norc", "-c", command],
                cwd=str(cwd),
                encoding="utf-8",
                echo=False,
                env=os.environ.copy(),
            )
        )


class OutputBuffer:
    def __init__(self, max_chars: int = OUTPUT_MAX_CHARS) -> None:
        self.max_chars = max_chars
        self._text = ""
        self._start_offset = 0
        self._total_chars = 0
        self._truncated = False

    @property
    def total_chars(self) -> int:
        return self._total_chars

    @property
    def truncated(self) -> bool:
        return self._truncated

    def append(self, text: str) -> None:
        if not text:
            return
        self._text += text
        self._total_chars += len(text)
        overflow = len(self._text) - self.max_chars
        if overflow > 0:
            self._text = self._text[overflow:]
            self._start_offset += overflow
            self._truncated = True

    def snapshot_since(self, offset: int, max_chars: int) -> OutputSnapshot:
        clamped_offset = max(offset, self._start_offset)
        local_start = clamped_offset - self._start_offset
        output = self._text[local_start:]
        truncated = self._truncated or offset < self._start_offset
        if len(output) > max_chars:
            output = output[-max_chars:]
            truncated = True
        if offset < self._start_offset:
            output = (
                f"... [truncated, {self._start_offset - offset} earlier chars]\n"
                + output
            )
        return OutputSnapshot(
            output=output,
            next_offset=self._total_chars,
            original_char_count=self._total_chars,
            truncated=truncated,
        )

    def snapshot_all(self, max_chars: int) -> OutputSnapshot:
        return self.snapshot_since(0, max_chars)


@dataclass
class ManagedTerminal:
    session_id: int
    command: str
    cwd: Path
    session: TerminalSession
    started_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)
    output_buffer: OutputBuffer = field(default_factory=OutputBuffer)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    output_event: asyncio.Event = field(default_factory=asyncio.Event)
    exit_code: int | None = None
    reader_task: asyncio.Task[None] | None = None
    last_returned_offset: int = 0
    _reader_started: bool = False

    @property
    def alive(self) -> bool:
        return self.exit_code is None and self.session.is_alive()

    def start_reader(self) -> None:
        if self._reader_started:
            return
        self._reader_started = True
        self.reader_task = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self) -> None:
        try:
            while True:
                read = await asyncio.to_thread(
                    self.session.read_chunk,
                    READ_IDLE_TIMEOUT_S,
                )
                if read.output:
                    self.output_buffer.append(read.output)
                    self.output_event.set()
                if not read.alive:
                    self.exit_code = read.exit_code
                    self.output_event.set()
                    return
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.output_buffer.append(f"\n[reader error: {exc}]\n")
            self.exit_code = self.session.exit_code()
            self.output_event.set()

    async def wait_for_activity(self, timeout_s: float) -> None:
        if timeout_s <= 0:
            return
        if self.exit_code is not None:
            return
        self.output_event.clear()
        try:
            await asyncio.wait_for(self.output_event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return

    async def wait_until_exit_or_timeout(self, timeout_s: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while self.exit_code is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.output_event.clear()
            try:
                await asyncio.wait_for(self.output_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return

    async def wait_until_quiet_or_exit(self, timeout_s: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout_s)
        saw_output = self.output_buffer.total_chars > self.last_returned_offset

        while self.exit_code is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            wait_s = min(remaining, YIELD_AFTER_IDLE_S if saw_output else remaining)
            self.output_event.clear()
            try:
                await asyncio.wait_for(self.output_event.wait(), timeout=wait_s)
            except asyncio.TimeoutError:
                if saw_output:
                    return
                return
            if self.output_buffer.total_chars > self.last_returned_offset:
                saw_output = True

    async def write(self, data: str) -> None:
        await asyncio.to_thread(self.session.write, data)

    async def terminate(self) -> None:
        await asyncio.to_thread(self.session.terminate)
        self.exit_code = self.session.exit_code()
        self.output_event.set()
        if self.reader_task is not None and not self.reader_task.done():
            self.reader_task.cancel()
            try:
                await self.reader_task
            except asyncio.CancelledError:
                pass

    def snapshot_since_last(self, max_chars: int) -> OutputSnapshot:
        snapshot = self.output_buffer.snapshot_since(self.last_returned_offset, max_chars)
        self.last_returned_offset = snapshot.next_offset
        return snapshot

    def info(self) -> SessionInfo:
        return SessionInfo(
            session_id=self.session_id,
            command=self.command,
            cwd=self.cwd,
            started_at=self.started_at,
            last_used_at=self.last_used_at,
            exit_code=self.exit_code,
            alive=self.alive,
            output_chars=self.output_buffer.total_chars,
            truncated=self.output_buffer.truncated,
        )


class ProcessManager:
    def __init__(
        self,
        backend: TerminalBackend | None = None,
        *,
        max_processes: int = MAX_PROCESSES,
    ) -> None:
        self._backend = backend or PexpectTerminalBackend()
        self._max_processes = max_processes
        self._lock = asyncio.Lock()
        self._next_id = itertools.count(1001)
        self._sessions: dict[int, ManagedTerminal] = {}

    async def allocate_id(self) -> int:
        async with self._lock:
            return next(self._next_id)

    async def spawn(self, session_id: int, command: str, cwd: Path) -> ManagedTerminal:
        session = await asyncio.to_thread(self._backend.spawn, command, cwd)
        managed = ManagedTerminal(
            session_id=session_id,
            command=command,
            cwd=cwd,
            session=session,
        )
        managed.start_reader()
        return managed

    async def store(self, managed: ManagedTerminal) -> None:
        async with self._lock:
            managed.last_used_at = time.monotonic()
            self._sessions[managed.session_id] = managed
            prune_targets = self._select_prune_targets_unlocked()
        await asyncio.gather(
            *(target.terminate() for target in prune_targets),
            return_exceptions=True,
        )

    async def get(self, session_id: int) -> ManagedTerminal | None:
        async with self._lock:
            managed = self._sessions.get(session_id)
            if managed is not None:
                managed.last_used_at = time.monotonic()
            return managed

    async def remove(self, session_id: int) -> ManagedTerminal | None:
        async with self._lock:
            return self._sessions.pop(session_id, None)

    async def terminate(self, session_id: int) -> bool:
        managed = await self.remove(session_id)
        if managed is None:
            return False
        await managed.terminate()
        return True

    async def terminate_all(self) -> None:
        async with self._lock:
            managed_sessions = list(self._sessions.values())
            self._sessions.clear()
        await asyncio.gather(
            *(managed.terminate() for managed in managed_sessions),
            return_exceptions=True,
        )

    async def list_sessions(self) -> list[SessionInfo]:
        async with self._lock:
            return [managed.info() for managed in self._sessions.values()]

    async def prune_exited(self) -> None:
        async with self._lock:
            exited_ids = [
                session_id
                for session_id, managed in self._sessions.items()
                if managed.exit_code is not None
            ]
            for session_id in exited_ids:
                self._sessions.pop(session_id, None)

    async def session_count(self) -> int:
        async with self._lock:
            return len(self._sessions)

    def _select_prune_targets_unlocked(self) -> list[ManagedTerminal]:
        if len(self._sessions) <= self._max_processes:
            return []

        overflow = len(self._sessions) - self._max_processes
        candidates = sorted(
            self._sessions.values(),
            key=lambda managed: (
                managed.exit_code is None,
                managed.last_used_at,
            ),
        )
        targets = candidates[:overflow]
        for target in targets:
            self._sessions.pop(target.session_id, None)
        return targets


process_manager = ProcessManager()
