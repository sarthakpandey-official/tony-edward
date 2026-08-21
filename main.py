"""
main.py — Tony-EDWARD entrypoint (v3 — Backblaze B2 + admin dashboard).

Boots FastAPI, wires all v1 routes + admin dashboard, installs the zero-log
policy, starts the auto-purge + sandbox cron background tasks, and initializes
Sentry.io for error monitoring + performance tracing.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import Settings, get_settings
from app.core.zero_log_middleware import ZeroLogMiddleware, install_zero_log_policy
from app.core.terminal_exec import heartbeat_sweep
from app.storage.auto_purge import get_auto_purge, reset_auto_purge
from app.storage.pattern_db_cache import ensure_schema
from app.engine.sandbox_cron import get_sandbox_cron, reset_sandbox_cron
from app.api import router as v1_router
from app.admin.dashboard import mount as mount_admin_dashboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("tony_edward.main")

install_zero_log_policy()


# ---------------------------------------------------------------------------
# Sentry.io initialization — error monitoring + performance tracing
# ---------------------------------------------------------------------------

# Headers / params to scrub before sending any data to Sentry servers.
# Even though Zero-Log Policy keeps them out of our own logs, we MUST
# also scrub them at the Sentry boundary so they never leave our process.
_SENSITIVE_HEADER_KEYS = {
    "authorization", "x-api-key", "x-tony-edward-llm-key", "x-tony-edward-llm-url",
    "cookie", "set-cookie", "x-auth-token", "x-admin-token", "admin_secret_key_64",
    "super_admin_key", "jwt_secret", "primary_llm_api_key", "primary_llm_api_key_fallback",
    "b2_application_key", "b2_application_key_id", "render_api_key",
}
_SENSITIVE_PARAM_KEYS = {
    "admin_key", "password", "secret", "token", "api_key", "apikey", "byo_api_key",
    "byo_api_url", "raw_token", "key", "private_key",
}


def _scrub_headers(headers):
    """Replace sensitive header values with [Filtered] before sending to Sentry."""
    if not isinstance(headers, dict):
        return headers
    scrubbed = {}
    for k, v in headers.items():
        if k.lower() in _SENSITIVE_HEADER_KEYS:
            scrubbed[k] = "[Filtered]"
        else:
            scrubbed[k] = v
    return scrubbed


def _scrub_params(params):
    """Replace sensitive request parameter values with [Filtered]."""
    if not isinstance(params, dict):
        return params
    scrubbed = {}
    for k, v in params.items():
        key_lower = k.lower() if isinstance(k, str) else str(k).lower()
        if key_lower in _SENSITIVE_PARAM_KEYS or any(s in key_lower for s in _SENSITIVE_PARAM_KEYS):
            scrubbed[k] = "[Filtered]"
        else:
            scrubbed[k] = v
    return scrubbed


def _sentry_before_send(event, hint):
    """Filter callback for Sentry: drop 401/403 events, scrub sensitive data.

    - HTTP 401 (Unauthorized) and 403 (Forbidden) are routine auth failures,
      not bugs. We drop them so they don't pollute Sentry.
    - For everything else, scrub sensitive headers + params before sending.
    """
    try:
        # Drop 401/403 events entirely
        exc_info = hint.get("exc_info") if hint else None
        if exc_info:
            from starlette.exceptions import HTTPException as StarletteHTTPException
            from fastapi import HTTPException as FastAPIHTTPException
            for exc in exc_info:
                exc_value = exc[1] if isinstance(exc, tuple) else exc
                if isinstance(exc_value, (FastAPIHTTPException, StarletteHTTPException)):
                    if getattr(exc_value, "status_code", 0) in (401, 403):
                        return None

        # Also check the event-level 'exception' values
        exceptions = event.get("exception", {}).get("values", [])
        for ex in exceptions:
            type_str = (ex.get("type") or "").lower()
            if "httpexception" in type_str or "http_exception" in type_str:
                value_str = (ex.get("value") or "").lower()
                if "401" in value_str or "403" in value_str:
                    return None

        # Scrub request headers + params
        request = event.get("request", {})
        if request:
            request["headers"] = _scrub_headers(request.get("headers"))
            request["cookies"] = "[Filtered]" if request.get("cookies") else request.get("cookies")
            request["query_string"] = "[Filtered]" if request.get("query_string") else request.get("query_string")
            if "data" in request:
                request["data"] = "[Filtered]"

        # Scrub extra contexts that may contain headers
        contexts = event.get("contexts", {})
        if "trace" in contexts and isinstance(contexts["trace"], dict):
            contexts["trace"].pop("headers", None)

        # Scrub breadcrumbs that may have captured headers
        for crumb in event.get("breadcrumbs", {}).get("values", []):
            if isinstance(crumb.get("data"), dict):
                crumb["data"] = _scrub_params(crumb["data"])

        # Scrub 'extra' top-level
        if "extra" in event and isinstance(event["extra"], dict):
            event["extra"] = _scrub_params(event["extra"])

        return event
    except Exception:
        # If our scrubber crashes, drop the event rather than leak data
        return None


def _sentry_before_send_transaction(event, hint):
    """Filter callback for Sentry transactions: scrub sensitive headers/params.

    Transactions contain request data for performance traces. We must
    scrub Authorization, X-API-Key, Cookie etc. before they leave.
    """
    try:
        request = event.get("request", {})
        if request:
            request["headers"] = _scrub_headers(request.get("headers"))
            request["cookies"] = "[Filtered]" if request.get("cookies") else request.get("cookies")
            if "data" in request:
                request["data"] = "[Filtered]"

        tags = event.get("tags", {})
        if isinstance(tags, dict):
            event["tags"] = _scrub_params(tags)

        if "extra" in event and isinstance(event["extra"], dict):
            event["extra"] = _scrub_params(event["extra"])

        return event
    except Exception:
        return None


def init_sentry(settings):
    """Initialize Sentry SDK with FastAPI integration + data sanitization."""
    if not settings.sentry_dsn:
        logger.info("sentry_disabled_no_dsn")
        return
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,  # NEVER send PII
        attach_stacktrace=True,
        max_breadcrumbs=50,
        integrations=[
            StarletteIntegration(),
            FastApiIntegration(),
        ],
        before_send=_sentry_before_send,
        before_send_transaction=_sentry_before_send_transaction,
    )
    logger.info("sentry_initialized env=%s traces_sample_rate=%.2f",
                settings.environment, settings.sentry_traces_sample_rate)


# Initialize Sentry BEFORE lifespan — errors during boot will be captured
init_sentry(get_settings())


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    os.makedirs(settings.storage_dir, exist_ok=True)
    ensure_schema(settings)

    if not os.environ.get("SUPER_ADMIN_KEY_PRINTED"):
        logger.warning("=" * 60)
        if settings.admin_key_is_strict_64:
            logger.warning("ADMIN_SECRET_KEY_64=<set, 64-char, strict>")
        else:
            logger.warning("WARNING: ADMIN_SECRET_KEY_64 not set or too short")
            logger.warning("AUTO-GENERATED (LOCAL DEV ONLY): %s", settings.admin_secret_key_64)
        logger.warning("Legacy SUPER_ADMIN_KEY=%s", settings.super_admin_key)
        logger.warning("Save these keys NOW. They will not be printed again.")
        logger.warning("=" * 60)
        os.environ["SUPER_ADMIN_KEY_PRINTED"] = "1"

    reset_auto_purge()
    reset_sandbox_cron()

    purge = get_auto_purge(settings)
    await purge.start()
    heartbeat_task = asyncio.create_task(heartbeat_sweep())

    sandbox = get_sandbox_cron(settings)
    await sandbox.start()

    logger.info("tony_edward_started v3 env=%s port=%d storage=%s admin_strict_64=%s b2=%s",
                settings.environment, settings.port, settings.storage_dir,
                settings.admin_key_is_strict_64, settings.b2_configured)

    yield

    logger.info("tony_edward_shutting_down")
    await sandbox.stop()
    await purge.stop()
    heartbeat_task.cancel()
    from app.scrapers.base import close_http_client
    await close_http_client()


app = FastAPI(
    title="Tony-EDWARD",
    description=(
        "Predictive B2B intelligence system on Render.com. "
        "v3: Backblaze B2 storage, LLM auto-failover, bcrypt end-user keys, "
        "30-day sandbox cron, super-admin web dashboard."
    ),
    version="3.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.add_middleware(ZeroLogMiddleware)


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {
        "status": "ok",
        "service": "tony-edward",
        "version": "3.0.0",
        "timestamp": time.time(),
    }


@app.get("/", tags=["meta"])
async def root() -> dict:
    return {
        "service": "Tony-EDWARD",
        "version": "3.0.0",
        "description": "Predictive B2B intelligence system",
        "docs": "/docs",
        "health": "/health",
        "admin_dashboard": "/admin/",
        "v1_prefix": "/v1",
    }


@app.exception_handler(Exception)
async def global_exc_handler(request: Request, exc: Exception):
    logger.exception("unhandled_exception path=%s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "internal_error", "request_id": request.headers.get("X-Request-ID", "anon")},
    )


app.include_router(v1_router, prefix="/v1")
mount_admin_dashboard(app)


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
        workers=1,
    )
