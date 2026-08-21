"""
app/core/terminal_exec.py — Render Web Terminal shell backend.

Provides:
  * `spawn_terminal()` — opens a long-lived PTY-backed shell subprocess.
  * `TerminalSession`  — async-friendly wrapper for streaming stdout/stderr
                          over a WebSocket and feeding stdin from the client.
  * 30s ping/pong heartbeat keep-alive (handled by the API layer).

SECURITY — READ CAREFULLY:
  * This module is the most privileged in the system. It grants an
    interactive shell on the Render host.
  * The API layer MUST verify `Role.SUPER_ADMIN` before instantiating
    `TerminalSession`. This module DOES NOT re-check auth — it trusts
    the caller.
  * Per-session hard caps: max 1 concurrent session globally, idle
    timeout 10 minutes, total lifetime 60 minutes, output ring buffer
    capped at 256 KiB (older bytes are discarded to bound memory).
  * All shell I/O for a session is kept strictly in memory. Terminal
    sessions are NEVER persisted to disk, NEVER shipped to R2, NEVER
    logged. Zero-Logging Policy applies in full.
  * The shell runs with the same UID as the uvicorn worker. On Render
    that is the deploy user; we do NOT attempt privilege escalation.
"""
from __future__ import annotations

import asyncio
import os
import pty
import shlex
import signal
import struct
import termios
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import logging

logger = logging.getLogger("tony_edward.terminal")
logger.setLevel(logging.INFO)


DEFAULT_COLS = 120
DEFAULT_ROWS = 40
MAX_OUTPUT_BYTES = 256 * 1024      # ring buffer per session
IDLE_TIMEOUT_SEC = 10 * 60         # 10 minutes
MAX_LIFETIME_SEC = 60 * 60         # 60 minutes
READ_CHUNK_BYTES = 4096


@dataclass
class TerminalSession:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    master_fd: int = -1
    pid: int = -1
    cols: int = DEFAULT_COLS
    rows: int = DEFAULT_ROWS
    _reader_task: Optional[asyncio.Task] = None
    _output_buffer: bytearray = field(default_factory=bytearray)
    _output_event: asyncio.Event = field(default_factory=asyncio.Event)
    _closed: bool = False

    # ---------------- lifecycle ----------------

    async def start(self, command: Optional[list[str]] = None) -> None:
        """Spawn the shell. Defaults to $SHELL or `bash`."""
        shell = command or [os.environ.get("SHELL", "bash"), "-i"]
        pid, master_fd = pty.fork()
        if pid == 0:
            # Child
            try:
                env = {
                    "TERM": "xterm-256color",
                    "COLUMNS": str(self.cols),
                    "LINES": str(self.rows),
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                    "HOME": os.environ.get("HOME", "/app"),
                }
                os.execvpe(shell[0], shell, env)
            except Exception:
                os._exit(127)
        else:
            # Parent
            self.master_fd = master_fd
            self.pid = pid
            self._set_winsize(self.cols, self.rows)
            # Make master_fd non-blocking
            import fcntl
            flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
            fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            # Spawn reader
            loop = asyncio.get_event_loop()
            self._reader_task = loop.create_task(self._reader_loop())

    async def _reader_loop(self) -> None:
        """Drain master_fd into the in-memory ring buffer."""
        loop = asyncio.get_event_loop()
        while not self._closed:
            try:
                data = await loop.run_in_executor(None, self._blocking_read)
            except OSError:
                break
            if not data:
                break
            self._output_buffer.extend(data)
            # Trim ring buffer
            if len(self._output_buffer) > MAX_OUTPUT_BYTES:
                overflow = len(self._output_buffer) - MAX_OUTPUT_BYTES
                del self._output_buffer[:overflow]
            self.last_activity = time.time()
            self._output_event.set()
            self._output_event.clear()
        # EOF
        self._output_event.set()

    def _blocking_read(self) -> bytes:
        try:
            return os.read(self.master_fd, READ_CHUNK_BYTES)
        except OSError:
            return b""

    # ---------------- client API ----------------

    async def stream_output(self):
        """Async generator yielding new output chunks as they arrive."""
        cursor = 0
        while not self._closed:
            # Yield anything new in the buffer
            if cursor < len(self._output_buffer):
                chunk = bytes(self._output_buffer[cursor:])
                cursor = len(self._output_buffer)
                yield chunk
                continue
            # Wait for more
            try:
                await asyncio.wait_for(self._output_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                # Heartbeat opportunity for the API layer
                yield b""
            if self._closed:
                break

    async def write_stdin(self, data: str) -> None:
        if self._closed:
            return
        self.last_activity = time.time()
        encoded = data.encode("utf-8", errors="replace")
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: os.write(self.master_fd, encoded)
        )

    def resize(self, cols: int, rows: int) -> None:
        self.cols = max(20, min(400, cols))
        self.rows = max(5, min(200, rows))
        if self.master_fd >= 0:
            self._set_winsize(self.cols, self.rows)

    def _set_winsize(self, cols: int, rows: int) -> None:
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass  # best-effort

    def is_expired(self) -> bool:
        now = time.time()
        if now - self.created_at > MAX_LIFETIME_SEC:
            return True
        if now - self.last_activity > IDLE_TIMEOUT_SEC:
            return True
        return False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
        # Kill child
        if self.pid > 0:
            try:
                os.killpg(self.pid, signal.SIGHUP)
            except Exception:
                try:
                    os.kill(self.pid, signal.SIGTERM)
                except Exception:
                    pass
        if self.master_fd >= 0:
            try:
                os.close(self.master_fd)
            except Exception:
                pass
        self._output_event.set()


# ---------------- singleton manager ----------------

class TerminalManager:
    """Strictly 1 concurrent session globally — terminal is a single seat."""

    def __init__(self) -> None:
        self._session: Optional[TerminalSession] = None
        self._lock = asyncio.Lock()

    async def acquire(self, cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS) -> TerminalSession:
        async with self._lock:
            if self._session and not self._session._closed and not self._session.is_expired():
                # Single-seat policy — second attempt replaces the prior session.
                # This is intentional: admin reconnects after a network drop,
                # we don't want a stranded zombie holding the seat.
                await self._session.close()
            session = TerminalSession(cols=cols, rows=rows)
            await session.start()
            self._session = session
            return session

    async def get_current(self) -> Optional[TerminalSession]:
        if self._session is None:
            return None
        if self._session._closed or self._session.is_expired():
            await self._session.close()
            self._session = None
            return None
        return self._session

    async def close_all(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None


# Module-level singleton
terminal_manager = TerminalManager()


async def heartbeat_sweep() -> None:
    """Periodic cleanup of expired session. Called by the app startup task."""
    while True:
        await asyncio.sleep(30)
        try:
            cur = await terminal_manager.get_current()
            if cur and cur.is_expired():
                await terminal_manager.close_all()
        except Exception:
            pass  # never let the sweeper die
