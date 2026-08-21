"""
app/admin/dashboard.py — Super-Admin Web Dashboard (v3 — Backblaze B2).

Serves the single-page HTML/JS admin UI at `/admin/`. Auth via:
  * Cookie `tedw_admin` (HMAC-signed, HttpOnly, SameSite=Strict, 12h TTL), OR
  * Authorization: Bearer <ADMIN_SECRET_KEY_64> header

Routes:
  GET  /admin/                 — login form OR dashboard
  POST /admin/login            — validate 64-char key, set cookie
  POST /admin/logout           — clear cookie
  GET  /admin/api/metrics       — JSON live metrics (for SVG charts)
  GET  /admin/api/patterns      — recent patterns
  GET  /admin/api/b2/status      — Backblaze B2 status
  POST /admin/api/b2/backup     — backup pattern DB to B2
  POST /admin/api/b2/prune      — prune oldest B2 objects
  GET  /admin/api/keys          — list end-user keys
  POST /admin/api/keys          — create end-user key (returns raw token ONCE)
  DELETE /admin/api/keys/{id}   — revoke end-user key
  GET  /admin/api/sandbox/status    — next scheduled run + last summary
  POST /admin/api/sandbox/run       — trigger immediate sandbox pass
  GET  /admin/api/sandbox/history   — past run reports
  GET  /admin/api/render/service    — Render service info
  POST /admin/api/render/restart    — restart Render service
  POST /admin/api/render/scale      — scale to new plan
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.security import AuthContext
from app.storage import (
    total_count, storage_dir_size_gb, list_recent_patterns,
    b2_status, backup_pattern_db, b2_list_objects, b2_delete_objects,
)
from app.storage.pattern_db_cache import db_size_bytes
from app.engine.pattern_learning import stats as pattern_stats
from app.engine.sandbox_cron import get_sandbox_cron, list_run_reports
from app.core import bcrypt_auth

logger = logging.getLogger("tony_edward.dashboard")
logger.setLevel(logging.INFO)

COOKIE_NAME = "tedw_admin"
COOKIE_TTL_SEC = 12 * 3600
_login_failures: dict[str, list[float]] = {}

router = APIRouter()


# ----------------- cookie helpers -----------------

def _hmac_sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _make_cookie_value(secret: str) -> str:
    expires = int(time.time() + COOKIE_TTL_SEC)
    payload = str(expires)
    sig = _hmac_sign(payload, secret)
    return f"{payload}.{sig}"


def _verify_cookie(cookie_value: str, secret: str) -> bool:
    if not cookie_value or "." not in cookie_value:
        return False
    payload, sig = cookie_value.rsplit(".", 1)
    try:
        expires = int(payload)
    except ValueError:
        return False
    if expires < time.time():
        return False
    expected = _hmac_sign(payload, secret)
    return hmac.compare_digest(sig, expected)


def _get_admin_secret() -> str:
    settings = get_settings()
    return f"{settings.effective_admin_key}:{settings.jwt_secret}"


def _extract_admin_token(request: Request) -> Optional[str]:
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie and _verify_cookie(cookie, _get_admin_secret()):
        return cookie
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        settings = get_settings()
        if hmac.compare_digest(token.encode("utf-8"),
                               settings.effective_admin_key.encode("utf-8")):
            return token
    return None


def _require_admin(request: Request) -> bool:
    token = _extract_admin_token(request)
    if not token:
        accept = request.headers.get("accept", "")
        if "text/html" in accept and not request.url.path.startswith("/admin/api/"):
            raise HTTPException(status_code=303, headers={"Location": "/admin/"})
        raise HTTPException(status_code=401, detail="admin_auth_required")
    return True


# ----------------- HTML -----------------

_TEMPLATE_CACHE: Optional[str] = None


def _load_template() -> str:
    template_path = os.path.join(os.path.dirname(__file__), "templates", "dashboard.html")
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


def _get_template() -> str:
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is None:
        _TEMPLATE_CACHE = _load_template()
    return _TEMPLATE_CACHE


@router.get("/", response_class=HTMLResponse)
@router.get("", response_class=HTMLResponse)
async def dashboard_root(request: Request) -> HTMLResponse:
    token = _extract_admin_token(request)
    authed = "true" if token else "false"
    return HTMLResponse(content=_get_template().replace("__AUTHENTICATED__", authed))


@router.post("/login")
async def dashboard_login(request: Request, admin_key: str = Form(...)) -> Response:
    settings = get_settings()
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    recent_fails = [t for t in _login_failures.get(client_ip, []) if now - t < 60]
    if len(recent_fails) >= 5:
        raise HTTPException(status_code=429, detail="too_many_login_attempts")
    _login_failures[client_ip] = recent_fails

    if not hmac.compare_digest(admin_key.encode("utf-8"),
                              settings.effective_admin_key.encode("utf-8")):
        _login_failures.setdefault(client_ip, []).append(now)
        logger.warning("admin_login_failed ip=%s", client_ip)
        return RedirectResponse(url="/admin/?error=invalid_key", status_code=303)

    cookie_value = _make_cookie_value(_get_admin_secret())
    resp = RedirectResponse(url="/admin/", status_code=303)
    resp.set_cookie(
        key=COOKIE_NAME, value=cookie_value, max_age=COOKIE_TTL_SEC,
        httponly=True, secure=settings.is_production, samesite="strict", path="/admin",
    )
    logger.info("admin_login_success ip=%s", client_ip)
    return resp


@router.post("/logout")
async def dashboard_logout() -> Response:
    resp = RedirectResponse(url="/admin/", status_code=303)
    resp.delete_cookie(key=COOKIE_NAME, path="/admin")
    return resp


# ----------------- API -----------------

@router.get("/api/metrics")
async def api_metrics(request: Request) -> dict:
    _require_admin(request)
    settings = get_settings()
    try:
        import resource
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        mem_mb = rss_kb / 1024.0
    except Exception:
        mem_mb = 0.0
    return {
        "timestamp": time.time(),
        "environment": settings.environment,
        "storage": {
            "used_gb": round(storage_dir_size_gb(settings), 3),
            "limit_gb": settings.auto_purge_storage_limit_gb,
            "pattern_db_size_bytes": db_size_bytes(settings),
        },
        "patterns": pattern_stats(settings),
        "b2": b2_status(settings),
        "enduser_keys": bcrypt_auth.registry_stats(settings),
        "sandbox": get_sandbox_cron(settings).status(),
        "process": {
            "memory_mb": round(mem_mb, 2),
            "uptime_sec": round(time.time() - _APP_START_TIME, 1),
        },
    }


@router.get("/api/patterns")
async def api_patterns(request: Request, source: Optional[str] = None, limit: int = 50) -> dict:
    _require_admin(request)
    return {"patterns": list_recent_patterns(source=source, limit=limit)}


@router.get("/api/b2/status")
async def api_b2_status(request: Request) -> dict:
    _require_admin(request)
    return b2_status()


@router.post("/api/b2/backup")
async def api_b2_backup(request: Request) -> dict:
    _require_admin(request)
    res = backup_pattern_db()
    return {
        "ok": res.ok, "key": res.key, "size_bytes": res.size_bytes,
        "disabled": res.disabled, "error": res.error,
    }


@router.post("/api/b2/prune")
async def api_b2_prune(request: Request, fraction: float = 0.1) -> dict:
    """Prune the oldest B2 objects by fraction."""
    _require_admin(request)
    settings = get_settings()
    if not settings.b2_configured:
        return {"ok": False, "error": "b2_not_configured"}
    objs = b2_list_objects(prefix="patterns", settings=settings)
    objs.sort(key=lambda o: o.last_modified)
    n_to_delete = max(1, int(len(objs) * fraction))
    keys = [o.key for o in objs[:n_to_delete]]
    result = b2_delete_objects(keys, settings)
    return {"ok": result.get("ok", False), "deleted": result.get("deleted", 0), "errors": result.get("errors", [])}


@router.get("/api/keys")
async def api_keys_list(request: Request) -> dict:
    _require_admin(request)
    return {"keys": bcrypt_auth.list_keys()}


@router.post("/api/keys")
async def api_keys_create(request: Request) -> dict:
    _require_admin(request)
    settings = get_settings()
    body = await request.json()
    try:
        raw_token, entry = bcrypt_auth.create_key(
            label=body.get("label", ""),
            allowed_domains=body.get("allowed_domains", []),
            allowed_origins=body.get("allowed_origins", []),
            rate_per_minute=int(body.get("rate_per_minute", 20)),
            settings=settings,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "raw_token": raw_token, "key_id": entry.key_id,
        "warning": "Save this token now. It will not be retrievable again.",
    }


@router.delete("/api/keys/{key_id}")
async def api_keys_revoke(key_id: str, request: Request) -> dict:
    _require_admin(request)
    settings = get_settings()
    ok = bcrypt_auth.revoke_key(key_id, settings)
    if not ok:
        raise HTTPException(status_code=404, detail="key_not_found")
    return {"ok": True, "revoked": key_id}


@router.get("/api/sandbox/status")
async def api_sandbox_status(request: Request) -> dict:
    _require_admin(request)
    return get_sandbox_cron().status()


@router.post("/api/sandbox/run")
async def api_sandbox_run(request: Request) -> dict:
    _require_admin(request)
    cron = get_sandbox_cron()
    report = await cron.trigger_now()
    return {
        "ok": True,
        "patterns_evaluated": report.patterns_evaluated,
        "patterns_pruned_local": report.patterns_pruned_local,
        "patterns_pruned_b2_objects": report.patterns_pruned_b2_objects,
        "patterns_pruned_b2_lines": report.patterns_pruned_b2_lines,
        "avg_accuracy_before": round(report.avg_accuracy_before, 4),
        "avg_accuracy_after": round(report.avg_accuracy_after, 4),
        "duration_sec": round(report.duration_sec, 2),
        "accuracy_distribution": report.accuracy_distribution,
        "error": report.error,
    }


@router.get("/api/sandbox/history")
async def api_sandbox_history(request: Request, limit: int = 20) -> dict:
    _require_admin(request)
    return {"runs": list_run_reports(limit=limit)}


# ----------------- Render API -----------------

async def _render_api_call(method: str, path: str, json_body: Optional[dict] = None) -> tuple[int, dict]:
    import httpx
    settings = get_settings()
    if not settings.render_api_key:
        return 400, {"error": "render_api_key_not_configured"}
    headers = {
        "Authorization": f"Bearer {settings.render_api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    url = f"{settings.render_api_base}{path}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(method, url, headers=headers, json=json_body)
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}
        return resp.status_code, body
    except httpx.HTTPError as exc:
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/render/service")
async def api_render_service(request: Request) -> dict:
    _require_admin(request)
    settings = get_settings()
    if not settings.render_service_id:
        return {"ok": False, "error": "render_service_id_not_configured"}
    status_code, body = await _render_api_call("GET", f"/services/{settings.render_service_id}")
    return {"ok": status_code == 200, "status": status_code, "service": body}


@router.post("/api/render/restart")
async def api_render_restart(request: Request) -> dict:
    _require_admin(request)
    settings = get_settings()
    if not settings.render_service_id:
        return {"ok": False, "error": "render_service_id_not_configured"}
    status_code, body = await _render_api_call("POST", f"/services/{settings.render_service_id}/restart")
    return {"ok": status_code in (200, 202, 204), "status": status_code, "response": body}


@router.post("/api/render/scale")
async def api_render_scale(request: Request, plan: str = "starter") -> dict:
    _require_admin(request)
    settings = get_settings()
    if not settings.render_service_id:
        return {"ok": False, "error": "render_service_id_not_configured"}
    status_code, body = await _render_api_call(
        "PATCH", f"/services/{settings.render_service_id}",
        json_body={"service": {"plan": plan}},
    )
    return {"ok": status_code in (200, 202), "status": status_code, "plan": plan, "response": body}


_APP_START_TIME = time.time()


def mount(app: FastAPI) -> None:
    app.include_router(router, prefix="/admin", tags=["admin-dashboard"])
