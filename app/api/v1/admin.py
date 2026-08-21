"""
app/api/v1/admin.py — Super-admin only endpoints (v3 — B2 architecture).

Routes (all require SUPER_ADMIN role):
  GET    /v1/admin/status               — full system status
  GET    /v1/admin/patterns             — list recent patterns
  POST   /v1/admin/purge                — trigger immediate local purge
  POST   /v1/admin/vacuum               — vacuum SQLite DB
  GET    /v1/admin/b2/status             — Backblaze B2 status
  POST   /v1/admin/b2/backup             — backup pattern DB to B2
  POST   /v1/admin/b2/prune             — prune oldest B2 objects
  GET    /v1/admin/adapter/registry     — dynamic adapter registry stats
  POST   /v1/admin/adapter/override      — pin custom selectors for a host
  DELETE /v1/admin/adapter/override     — remove a host override
  POST   /v1/admin/export/finetune       — export patterns as fine-tune JSONL
  GET    /v1/admin/terminal/active       — is a terminal session active?

End-user API key management (bcrypt):
  GET    /v1/admin/keys                 — list end-user keys
  POST   /v1/admin/keys                  — create new end-user key
  DELETE /v1/admin/keys/{key_id}         — revoke an end-user key
  GET    /v1/admin/keys/stats            — registry stats

Sandbox cron:
  GET    /v1/admin/sandbox/status       — next scheduled run + last summary
  POST   /v1/admin/sandbox/run          — trigger immediate sandbox pass
  GET    /v1/admin/sandbox/history      — list past sandbox run reports

Render API:
  GET    /v1/admin/render/service        — Render service info
  POST   /v1/admin/render/restart        — restart the Render service
  POST   /v1/admin/render/scale          — scale to a new plan
  GET    /v1/admin/render/deploys        — list recent deploys
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.security import AuthContext, get_auth_context
from app.core import bcrypt_auth
from app.storage import (
    list_recent_patterns, total_count, storage_dir_size_gb,
    prune_low_novelty, vacuum, b2_status, backup_pattern_db,
    b2_list_objects, b2_delete_objects,
)
from app.storage.pattern_db_cache import db_size_bytes
from app.engine.pattern_learning import stats as pattern_stats, export_fine_tune_dataset
from app.engine.sandbox_cron import get_sandbox_cron, list_run_reports
from app.scrapers.dynamic_app_adapter import (
    registry_stats, set_admin_override, clear_admin_override,
)
from app.core.terminal_exec import terminal_manager

router = APIRouter()
logger = logging.getLogger("tony_edward.api.admin")
logger.setLevel(logging.INFO)


async def require_super_admin(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
    if not ctx.is_admin:
        raise HTTPException(status_code=403, detail="super_admin_required")
    return ctx


class PurgeRequest(BaseModel):
    target_count: Optional[int] = Field(None, ge=0)


class OverrideRequest(BaseModel):
    host: str = Field(..., min_length=3, max_length=200)
    app_type: str = Field("html_static")
    selectors: dict[str, str] = Field(default_factory=dict)


class OverrideDeleteRequest(BaseModel):
    host: str = Field(..., min_length=3, max_length=200)


class ExportRequest(BaseModel):
    source: Optional[str] = None
    limit: int = Field(10_000, ge=1, le=1_000_000)


class CreateKeyRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=100)
    allowed_domains: list[str] = Field(default_factory=list)
    allowed_origins: list[str] = Field(default_factory=list)
    rate_per_minute: int = Field(20, ge=1, le=10_000)


class ScaleRequest(BaseModel):
    plan: str = Field(...)


@router.get("/status")
async def status(ctx: AuthContext = Depends(require_super_admin)) -> dict:
    settings = get_settings()
    return {
        "environment": settings.environment,
        "port": settings.port,
        "admin_key_strict_64": settings.admin_key_is_strict_64,
        "storage": {
            "storage_dir": settings.storage_dir,
            "used_gb": round(storage_dir_size_gb(settings), 3),
            "limit_gb": settings.auto_purge_storage_limit_gb,
            "headroom_gb": round(
                settings.auto_purge_storage_limit_gb - storage_dir_size_gb(settings), 3
            ),
            "pattern_db_size_bytes": db_size_bytes(settings),
        },
        "patterns": pattern_stats(settings),
        "b2": b2_status(settings),
        "adapter": registry_stats(),
        "enduser_keys": bcrypt_auth.registry_stats(settings),
        "terminal_active": (await terminal_manager.get_current()) is not None,
        "sandbox": get_sandbox_cron(settings).status(),
    }


@router.get("/patterns")
async def list_patterns(
    source: Optional[str] = None,
    limit: int = 50,
    ctx: AuthContext = Depends(require_super_admin),
) -> dict:
    return {"patterns": list_recent_patterns(source=source, limit=limit)}


@router.post("/purge")
async def trigger_purge(req: PurgeRequest, ctx: AuthContext = Depends(require_super_admin)) -> dict:
    settings = get_settings()
    if req.target_count:
        deleted = prune_low_novelty(req.target_count, settings)
    else:
        from app.engine.pattern_learning import purge_stale
        deleted = purge_stale(settings)
    return {"deleted": deleted, "remaining": total_count(settings)}


@router.post("/vacuum")
async def trigger_vacuum(ctx: AuthContext = Depends(require_super_admin)) -> dict:
    settings = get_settings()
    before = db_size_bytes(settings)
    vacuum(settings)
    after = db_size_bytes(settings)
    return {"before_bytes": before, "after_bytes": after, "reclaimed_bytes": before - after}


@router.get("/b2/status")
async def b2_status_endpoint(ctx: AuthContext = Depends(require_super_admin)) -> dict:
    return b2_status()


@router.post("/b2/backup")
async def b2_backup(ctx: AuthContext = Depends(require_super_admin)) -> dict:
    result = backup_pattern_db()
    return {
        "ok": result.ok, "key": result.key, "size_bytes": result.size_bytes,
        "disabled": result.disabled, "error": result.error,
    }


@router.post("/b2/prune")
async def b2_prune(
    req: dict = None,
    ctx: AuthContext = Depends(require_super_admin),
) -> dict:
    """Prune oldest B2 pattern objects by fraction."""
    settings = get_settings()
    if not settings.b2_configured:
        return {"ok": False, "error": "b2_not_configured"}
    fraction = 0.1
    if req and "fraction" in req:
        fraction = float(req["fraction"])
    objs = b2_list_objects(prefix="patterns", settings=settings)
    objs.sort(key=lambda o: o.last_modified)
    n_to_delete = max(1, int(len(objs) * fraction))
    keys = [o.key for o in objs[:n_to_delete]]
    result = b2_delete_objects(keys, settings)
    return {"ok": result.get("ok", False), "deleted": result.get("deleted", 0), "errors": result.get("errors", [])}


@router.get("/adapter/registry")
async def adapter_registry(ctx: AuthContext = Depends(require_super_admin)) -> dict:
    return registry_stats()


@router.post("/adapter/override")
async def adapter_set_override(
    req: OverrideRequest,
    ctx: AuthContext = Depends(require_super_admin),
) -> dict:
    set_admin_override(req.host, req.selectors, req.app_type)
    return {"ok": True, "host": req.host, "app_type": req.app_type}


@router.delete("/adapter/override")
async def adapter_delete_override(
    req: OverrideDeleteRequest,
    ctx: AuthContext = Depends(require_super_admin),
) -> dict:
    clear_admin_override(req.host)
    return {"ok": True, "host": req.host}


@router.post("/export/finetune")
async def export_finetune(
    req: ExportRequest,
    ctx: AuthContext = Depends(require_super_admin),
) -> dict:
    settings = get_settings()
    export_dir = os.path.join(settings.storage_dir, "exports")
    os.makedirs(export_dir, exist_ok=True)
    out_path = os.path.join(export_dir, f"finetune_{int(time.time())}.jsonl")
    result = export_fine_tune_dataset(out_path, source=req.source, limit=req.limit)
    return result


@router.get("/keys")
async def list_enduser_keys(ctx: AuthContext = Depends(require_super_admin)) -> dict:
    settings = get_settings()
    return {"keys": bcrypt_auth.list_keys(settings)}


@router.post("/keys")
async def create_enduser_key(
    req: CreateKeyRequest,
    ctx: AuthContext = Depends(require_super_admin),
) -> dict:
    settings = get_settings()
    try:
        raw_token, entry = bcrypt_auth.create_key(
            label=req.label,
            allowed_domains=req.allowed_domains,
            allowed_origins=req.allowed_origins,
            rate_per_minute=req.rate_per_minute,
            settings=settings,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "raw_token": raw_token, "key_id": entry.key_id, "label": entry.label,
        "allowed_domains": entry.allowed_domains, "allowed_origins": entry.allowed_origins,
        "rate_per_minute": entry.rate_per_minute,
        "warning": "Save this token now. It will not be retrievable again.",
    }


@router.delete("/keys/{key_id}")
async def revoke_enduser_key(
    key_id: str,
    ctx: AuthContext = Depends(require_super_admin),
) -> dict:
    settings = get_settings()
    ok = bcrypt_auth.revoke_key(key_id, settings)
    if not ok:
        raise HTTPException(status_code=404, detail="key_not_found")
    return {"ok": True, "revoked": key_id}


@router.get("/keys/stats")
async def keys_stats(ctx: AuthContext = Depends(require_super_admin)) -> dict:
    return bcrypt_auth.registry_stats()


@router.get("/sandbox/status")
async def sandbox_status(ctx: AuthContext = Depends(require_super_admin)) -> dict:
    settings = get_settings()
    return get_sandbox_cron(settings).status()


@router.post("/sandbox/run")
async def sandbox_run(ctx: AuthContext = Depends(require_super_admin)) -> dict:
    settings = get_settings()
    cron = get_sandbox_cron(settings)
    report = await cron.trigger_now()
    return {
        "started_at": report.started_at, "finished_at": report.finished_at,
        "duration_sec": round(report.duration_sec, 2),
        "patterns_evaluated": report.patterns_evaluated,
        "patterns_pruned_local": report.patterns_pruned_local,
        "patterns_pruned_b2_objects": report.patterns_pruned_b2_objects,
        "patterns_pruned_b2_lines": report.patterns_pruned_b2_lines,
        "avg_accuracy_before": round(report.avg_accuracy_before, 4),
        "avg_accuracy_after": round(report.avg_accuracy_after, 4),
        "accuracy_distribution": report.accuracy_distribution,
        "error": report.error,
    }


@router.get("/sandbox/history")
async def sandbox_history(
    limit: int = 20,
    ctx: AuthContext = Depends(require_super_admin),
) -> dict:
    settings = get_settings()
    return {"runs": list_run_reports(settings, limit=limit)}


# --- Render API ---

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


@router.get("/render/service")
async def render_service(ctx: AuthContext = Depends(require_super_admin)) -> dict:
    settings = get_settings()
    if not settings.render_service_id:
        return {"ok": False, "error": "render_service_id_not_configured"}
    status_code, body = await _render_api_call("GET", f"/services/{settings.render_service_id}")
    return {"ok": status_code == 200, "status": status_code, "service": body}


@router.post("/render/restart")
async def render_restart(ctx: AuthContext = Depends(require_super_admin)) -> dict:
    settings = get_settings()
    if not settings.render_service_id:
        return {"ok": False, "error": "render_service_id_not_configured"}
    status_code, body = await _render_api_call("POST", f"/services/{settings.render_service_id}/restart")
    return {"ok": status_code in (200, 202, 204), "status": status_code, "response": body}


@router.post("/render/scale")
async def render_scale(
    req: ScaleRequest,
    ctx: AuthContext = Depends(require_super_admin),
) -> dict:
    settings = get_settings()
    if not settings.render_service_id:
        return {"ok": False, "error": "render_service_id_not_configured"}
    status_code, body = await _render_api_call(
        "PATCH", f"/services/{settings.render_service_id}",
        json_body={"service": {"plan": req.plan}},
    )
    return {"ok": status_code in (200, 202), "status": status_code, "plan": req.plan, "response": body}


@router.get("/render/deploys")
async def render_deploys(ctx: AuthContext = Depends(require_super_admin)) -> dict:
    settings = get_settings()
    if not settings.render_service_id:
        return {"ok": False, "error": "render_service_id_not_configured"}
    status_code, body = await _render_api_call(
        "GET", f"/services/{settings.render_service_id}/deploys?limit=20"
    )
    return {"ok": status_code == 200, "status": status_code, "deploys": body}


@router.get("/terminal/active")
async def terminal_active(ctx: AuthContext = Depends(require_super_admin)) -> dict:
    cur = await terminal_manager.get_current()
    if cur is None:
        return {"active": False}
    return {
        "active": True,
        "session_id": cur.session_id,
        "uptime_sec": round(time.time() - cur.created_at, 1),
        "last_activity_sec_ago": round(time.time() - cur.last_activity, 1),
    }
