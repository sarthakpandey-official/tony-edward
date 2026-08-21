"""
main.py — Tony-EDWARD entrypoint (v3 — Backblaze B2 + admin dashboard).

Boots FastAPI, wires all v1 routes + admin dashboard, installs the zero-log
policy, starts the auto-purge + sandbox cron background tasks.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
