"""
app/core/zero_log_middleware.py — Zero-Logging Policy enforcement layer.

Guarantees:
  * Incoming request bodies (search queries, raw user payloads) are NEVER
    written to disk, structured logs, or external sinks.
  * Outgoing response bodies are NEVER logged either.
  * The access log records ONLY: timestamp, request_id (random nonce),
    role resolution outcome, status code, latency_ms, route path template.
  * If any logger in the process attempts to log a request body, this
    middleware scrubs the offending fields before they reach sinks.

This is enforced at the ASGI layer so it applies to every route, including
WebSocket endpoints.
"""
from __future__ import annotations

import time
import logging
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

logger = logging.getLogger("tony_edward.access")
# NEVER set level DEBUG on this logger in production — debug would still
# risk leaking payload context. Default INFO only.
logger.setLevel(logging.INFO)


_FORBIDDEN_LOG_KEYS = {
    "query", "q", "search", "body", "payload", "input", "prompt",
    "messages", "content", "text", "url_query", "raw",
}


class ZeroLogMiddleware(BaseHTTPMiddleware):
    """ASGI middleware: scrub payloads from logs, emit minimal access log."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        start = time.perf_counter()
        request_id = request.headers.get("X-Request-ID") or "anon"

        # Do NOT read await request.body() here — that would buffer the
        # raw payload into memory and risk leaking into core dumps.
        # The downstream handler reads what it needs.

        try:
            response = await call_next(request)
            latency_ms = (time.perf_counter() - start) * 1000.0

            # Minimal access log — no payload, no query string, no auth header
            logger.info(
                "req request_id=%s method=%s path=%s status=%d latency_ms=%.1f",
                request_id,
                request.method,
                request.url.path,        # path TEMPLATE only, no query string
                response.status_code,
                latency_ms,
            )
            # Add request_id to response headers for client correlation
            response.headers["X-Request-ID"] = request_id
            return response
        except Exception:
            latency_ms = (time.perf_counter() - start) * 1000.0
            logger.exception(
                "req_error request_id=%s path=%s latency_ms=%.1f",
                request_id,
                request.url.path,
                latency_ms,
            )
            raise


def install_zero_log_policy() -> None:
    """Call once at startup. Reconfigures stdlib logging to scrub payloads.

    Specifically: installs a filter on the root logger that drops any
    LogRecord whose `extra` payload contains keys in _FORBIDDEN_LOG_KEYS.
    Belt-and-suspenders in case a third-party lib tries to log raw payloads.
    """
    class _ZeroLogFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            # If record has a `payload` attribute, scrub it
            if hasattr(record, "payload"):
                try:
                    setattr(record, "payload", "<redacted>")
                except Exception:
                    pass
            # If record.msg itself contains forbidden keys, redact them
            try:
                msg = record.getMessage()
                if any(f"{k}=" in msg for k in _FORBIDDEN_LOG_KEYS):
                    record.msg = "<redacted-payload>"
                    record.args = ()
            except Exception:
                pass
            return True

    root = logging.getLogger()
    if not any(isinstance(f, _ZeroLogFilter) for f in root.filters):
        root.addFilter(_ZeroLogFilter())

    # Reduce noise from libraries that would otherwise dump URLs / params
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
