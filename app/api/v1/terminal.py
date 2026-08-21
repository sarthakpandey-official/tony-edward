"""
app/api/v1/terminal.py — Render Web Terminal (WebSocket).

Endpoint:  WS /v1/terminal

Protocol:
  Client → Server messages (JSON):
    {"type": "auth",     "token": "<super_admin_key>"}     — required first message
    {"type": "input",    "data": "ls -la\\n"}
    {"type": "resize",  "cols": 120, "rows": 40}
    {"type": "ping"}
  Server → Client messages (JSON):
    {"type": "auth_ok"}
    {"type": "auth_failed"}
    {"type": "output",   "data": "<stdout/stderr chunk>"}
    {"type": "pong",     "ts": <epoch_ms>}
    {"type": "closed"}
    {"type": "error",    "msg": "..."}

Security:
  * Auth required: Role.SUPER_ADMIN only.
  * 30s ping/pong keep-alive.
  * Single concurrent session (single-seat policy enforced by TerminalManager).
  * 10-minute idle timeout, 60-minute max lifetime.
  * Output ring buffer capped at 256 KiB per session.
  * ZERO-LOG: terminal I/O is NEVER written to disk, never logged, never
    sent to R2. Output is streamed in-memory only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.core.config import get_settings
from app.core.security import Role
from app.core.terminal_exec import terminal_manager, TerminalSession

router = APIRouter()
logger = logging.getLogger("tony_edward.api.terminal")
logger.setLevel(logging.INFO)


PING_INTERVAL_SEC = 30.0
AUTH_TIMEOUT_SEC = 10.0


async def _send_json(ws: WebSocket, obj: dict) -> None:
    if ws.client_state != WebSocketState.CONNECTED:
        return
    try:
        await ws.send_text(json.dumps(obj))
    except Exception:
        pass  # client gone


async def _send_pong(ws: WebSocket, ts: float) -> None:
    await _send_json(ws, {"type": "pong", "ts": ts})


@router.websocket("")
@router.websocket("/")
async def terminal_endpoint(ws: WebSocket) -> None:
    """WebSocket terminal endpoint.

    The first message MUST be {"type":"auth","token":"<super_admin_key>"}.
    Without valid admin auth, the socket is closed immediately.
    """
    await ws.accept()
    settings = get_settings()
    super_admin_key = settings.super_admin_key

    # Auth phase
    try:
        first = await asyncio.wait_for(ws.receive_text(), timeout=AUTH_TIMEOUT_SEC)
        msg = json.loads(first)
        if msg.get("type") != "auth" or msg.get("token") != super_admin_key:
            await _send_json(ws, {"type": "auth_failed"})
            await ws.close(code=4401)
            return
    except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
        await _send_json(ws, {"type": "auth_failed", "reason": "auth_timeout_or_invalid"})
        try:
            await ws.close(code=4401)
        except Exception:
            pass
        return

    await _send_json(ws, {"type": "auth_ok"})

    # Acquire terminal
    session: TerminalSession
    try:
        session = await terminal_manager.acquire()
    except Exception as exc:
        await _send_json(ws, {"type": "error", "msg": f"terminal_spawn_failed: {exc}"})
        await ws.close(code=4500)
        return

    logger.info("terminal_session_started session_id=%s", session.session_id)

    # Spawn two background tasks: output streamer + ping keep-alive
    stop_event = asyncio.Event()

    async def stream_output() -> None:
        async for chunk in session.stream_output():
            if stop_event.is_set():
                return
            if chunk:
                await _send_json(ws, {"type": "output", "data": chunk.decode("utf-8", errors="replace")})
            else:
                # Heartbeat — check if expired
                if session.is_expired():
                    await _send_json(ws, {"type": "closed", "reason": "session_expired"})
                    return

    async def ping_loop() -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=PING_INTERVAL_SEC)
            except asyncio.TimeoutError:
                await _send_pong(ws, time.time())

    output_task = asyncio.create_task(stream_output())
    ping_task = asyncio.create_task(ping_loop())

    # Input loop
    try:
        while not stop_event.is_set():
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=60.0)
            except asyncio.TimeoutError:
                if session.is_expired():
                    await _send_json(ws, {"type": "closed", "reason": "idle_timeout"})
                    break
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _send_json(ws, {"type": "error", "msg": "invalid_json"})
                continue

            t = msg.get("type")
            if t == "input":
                await session.write_stdin(msg.get("data", ""))
            elif t == "resize":
                session.resize(int(msg.get("cols", 120)), int(msg.get("rows", 40)))
            elif t == "ping":
                await _send_pong(ws, time.time())
            elif t == "close":
                break
            else:
                await _send_json(ws, {"type": "error", "msg": f"unknown_type:{t}"})

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("terminal_loop_error err=%s", type(exc).__name__)
    finally:
        stop_event.set()
        output_task.cancel()
        ping_task.cancel()
        await terminal_manager.close_all()
        logger.info("terminal_session_ended session_id=%s", session.session_id)
        try:
            await ws.close(code=1000)
        except Exception:
            pass
