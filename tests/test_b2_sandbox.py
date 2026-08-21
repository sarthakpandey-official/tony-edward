"""
tests/test_b2_sandbox.py — Tests for Backblaze B2 + sandbox + bcrypt auth.

These tests do NOT hit real Backblaze B2 — they verify the in-process
logic (JSONL formatting, status shapes, sandbox engine, bcrypt auth).
For real B2 integration tests, set B2_APPLICATION_KEY_ID etc. in env.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("STORAGE_DIR", "/tmp/tony-edward-test")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("ADMIN_SECRET_KEY_64", "A" * 64)
os.environ.setdefault("SUPER_ADMIN_KEY", "tedw_sk_test_admin_key_1234567890")
os.environ.setdefault("JWT_SECRET", "test_jwt_secret_1234567890")
os.environ.setdefault("PRIMARY_LLM_API_KEY", "sk-test")
os.makedirs(os.environ["STORAGE_DIR"], exist_ok=True)


# ---------------------------------------------------------------------------
# B2 manager (no real B2 connection — just status checks when unconfigured)
# ---------------------------------------------------------------------------

def test_b2_status_dict_shape():
    from app.storage.b2_manager import status as b2_status
    s = b2_status()
    assert "configured" in s
    assert "bucket" in s
    assert "endpoint" in s
    assert "object_count" in s
    assert "estimated_size_gb" in s
    assert "limit_gb" in s
    assert "headroom_gb" in s
    assert "boto3_available" in s


def test_b2_status_disabled_when_no_creds():
    from app.storage.b2_manager import status as b2_status
    s = b2_status()
    # In test env, B2 creds are not set → configured should be False
    assert s["configured"] is False
    assert s["object_count"] == 0
    assert s["estimated_size_gb"] == 0.0
    assert s["limit_gb"] == 10


def test_b2_upload_returns_disabled_when_not_configured():
    from app.storage.b2_manager import upload_bytes
    res = upload_bytes(b"test data")
    assert res.ok is False
    assert res.disabled is True


def test_b2_list_objects_returns_empty_when_disabled():
    from app.storage.b2_manager import list_objects
    objs = list_objects()
    assert objs == []


# ---------------------------------------------------------------------------
# Pattern DB cache (B2 object key column)
# ---------------------------------------------------------------------------

def test_pattern_db_has_b2_object_key_column():
    """The patterns table must have a b2_object_key column for sandbox prune."""
    import sqlite3
    from app.storage.pattern_db_cache import ensure_schema
    from app.core.config import get_settings
    settings = get_settings()
    ensure_schema(settings)
    conn = sqlite3.connect(settings.pattern_db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(patterns)").fetchall()}
    conn.close()
    assert "b2_object_key" in cols
    assert "accuracy_score" in cols
    assert "last_evaluated_at" in cols


# ---------------------------------------------------------------------------
# Sandbox cron
# ---------------------------------------------------------------------------

def test_sandbox_cron_singleton():
    from app.engine.sandbox_cron import get_sandbox_cron, reset_sandbox_cron
    reset_sandbox_cron()
    c1 = get_sandbox_cron()
    c2 = get_sandbox_cron()
    assert c1 is c2


def test_sandbox_cron_status_shape():
    from app.engine.sandbox_cron import get_sandbox_cron
    s = get_sandbox_cron().status()
    assert "running" in s
    assert "enabled" in s
    assert "interval_days" in s
    assert "accuracy_threshold" in s
    assert "last_run" in s
    assert "next_run" in s


@pytest.mark.asyncio
async def test_sandbox_run_with_no_patterns_returns_skipped():
    """With an empty pattern DB, sandbox should report insufficient_patterns."""
    from app.engine.sandbox_cron import run_sandbox_pass
    from app.core.config import get_settings
    settings = get_settings()
    settings.__dict__["pattern_db_path"] = "/tmp/tony-edward-test/sandbox_empty.sqlite3"
    if os.path.exists(settings.pattern_db_path):
        os.remove(settings.pattern_db_path)
    report = await run_sandbox_pass(settings)
    assert report.error is not None
    assert "insufficient_patterns" in report.error


@pytest.mark.asyncio
async def test_sandbox_run_evaluates_labeled_patterns():
    """With labeled patterns, sandbox evaluates them and updates accuracy scores."""
    import sqlite3
    from app.engine.sandbox_cron import run_sandbox_pass
    from app.storage.pattern_db_cache import ensure_schema
    from app.core.config import get_settings
    settings = get_settings()
    settings.__dict__["pattern_db_path"] = "/tmp/tony-edward-test/sandbox_eval.sqlite3"
    if os.path.exists(settings.pattern_db_path):
        os.remove(settings.pattern_db_path)
    ensure_schema(settings)
    conn = sqlite3.connect(settings.pattern_db_path)
    now = time.time()
    for i in range(5):
        conn.execute(
            "INSERT INTO patterns (signature, source, task, vector, created_at, "
            "novelty_score, fine_tune_label, accuracy_score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f"sig_{i}", "test", "search", "[]", now - i * 100,
             0.5, "positive growth sentiment", 0.5),
        )
    conn.commit()
    conn.close()
    settings.__dict__["sandbox_min_patterns_to_eval"] = 3
    report = await run_sandbox_pass(settings)
    assert report.patterns_evaluated == 5
    assert sum(report.accuracy_distribution.values()) == 5


# ---------------------------------------------------------------------------
# Bcrypt auth
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def clean_bcrypt_cache():
    from app.core.bcrypt_auth import reset_cache
    from app.core.config import get_settings
    settings = get_settings()
    if os.path.exists(settings.enduser_api_keys_path):
        try:
            os.remove(settings.enduser_api_keys_path)
        except OSError:
            pass
    reset_cache()
    yield
    if os.path.exists(settings.enduser_api_keys_path):
        try:
            os.remove(settings.enduser_api_keys_path)
        except OSError:
            pass
    reset_cache()


def test_bcrypt_registry_stats_initial(clean_bcrypt_cache):
    from app.core.bcrypt_auth import registry_stats
    s = registry_stats()
    assert s["total_keys"] == 0
    assert s["active_keys"] == 0
    assert s["bcrypt_available"] is True


def test_bcrypt_create_and_verify_key(clean_bcrypt_cache):
    from app.core.bcrypt_auth import create_key, verify_token
    raw_token, entry = create_key(label="test_client", rate_per_minute=10)
    assert raw_token.startswith("tedw_uk_")
    assert entry.key_id.startswith("uk_")
    verified = verify_token(raw_token, origin=None, host=None)
    assert verified is not None
    assert verified.key_id == entry.key_id
    wrong = verify_token("tedw_uk_wrong_token_xyz", origin=None, host=None)
    assert wrong is None


def test_bcrypt_domain_binding(clean_bcrypt_cache):
    from app.core.bcrypt_auth import create_key, verify_token
    raw_token, _ = create_key(label="restricted", allowed_domains=["app.example.com"])
    ok = verify_token(raw_token, origin=None, host="app.example.com")
    assert ok is not None
    bad = verify_token(raw_token, origin=None, host="evil.com")
    assert bad is None


def test_bcrypt_origin_check(clean_bcrypt_cache):
    from app.core.bcrypt_auth import create_key, verify_token
    raw_token, _ = create_key(label="cors_restricted",
                               allowed_origins=["https://app.example.com"])
    ok = verify_token(raw_token, origin="https://app.example.com", host=None)
    assert ok is not None
    bad = verify_token(raw_token, origin="https://evil.com", host=None)
    assert bad is None


def test_bcrypt_revoke_key(clean_bcrypt_cache):
    from app.core.bcrypt_auth import create_key, revoke_key, verify_token
    raw_token, entry = create_key(label="to_revoke")
    assert verify_token(raw_token, origin=None, host=None) is not None
    assert revoke_key(entry.key_id) is True
    assert verify_token(raw_token, origin=None, host=None) is None


def test_bcrypt_list_keys_excludes_hashes(clean_bcrypt_cache):
    from app.core.bcrypt_auth import create_key, list_keys
    create_key(label="client_a")
    create_key(label="client_b")
    keys = list_keys()
    assert len(keys) == 2
    for k in keys:
        assert "bcrypt_hash" not in k
        assert "key_id" in k
        assert "label" in k


def test_bcrypt_raw_token_not_in_registry(clean_bcrypt_cache):
    """The raw token must NOT appear in the persisted registry file."""
    from app.core.bcrypt_auth import create_key
    from app.core.config import get_settings
    settings = get_settings()
    raw_token, _ = create_key(label="leak_test")
    if os.path.exists(settings.enduser_api_keys_path):
        with open(settings.enduser_api_keys_path) as f:
            content = f.read()
        assert raw_token not in content, "RAW TOKEN LEAKED INTO REGISTRY FILE"


# ---------------------------------------------------------------------------
# LLM auto-failover (admin role)
# ---------------------------------------------------------------------------

def test_llm_failover_config_loads_both_keys():
    """When both primary + fallback keys are set, config exposes both."""
    from app.core.config import get_settings, reload_settings
    os.environ["PRIMARY_LLM_API_KEY"] = "key1"
    os.environ["PRIMARY_LLM_API_KEY_FALLBACK"] = "key2"
    s = reload_settings()
    assert s.primary_llm_api_key == "key1"
    assert s.primary_llm_api_key_fallback == "key2"
    # Cleanup
    del os.environ["PRIMARY_LLM_API_KEY"]
    del os.environ["PRIMARY_LLM_API_KEY_FALLBACK"]
    reload_settings()


# ---------------------------------------------------------------------------
# Admin dashboard routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    import app.storage.auto_purge as ap_module
    from app.engine.sandbox_cron import reset_sandbox_cron
    from app.core.bcrypt_auth import reset_cache
    from app.core.config import get_settings
    ap_module._auto_purge = None
    reset_sandbox_cron()
    settings = get_settings()
    if os.path.exists(settings.enduser_api_keys_path):
        try:
            os.remove(settings.enduser_api_keys_path)
        except OSError:
            pass
    reset_cache()
    from fastapi.testclient import TestClient
    from main import app
    with TestClient(app) as c:
        yield c
    if ap_module._auto_purge is not None:
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(ap_module._auto_purge.stop())
            loop.close()
        except Exception:
            pass
    ap_module._auto_purge = None
    reset_sandbox_cron()
    reset_cache()
    if os.path.exists(settings.enduser_api_keys_path):
        try:
            os.remove(settings.enduser_api_keys_path)
        except OSError:
            pass


def test_admin_dashboard_login_page(client):
    """GET /admin/ returns the HTML page."""
    resp = client.get("/admin/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_admin_dashboard_rejects_api_call_without_auth(client):
    resp = client.get("/admin/api/metrics")
    assert resp.status_code == 401


def test_admin_dashboard_login_with_wrong_key_fails(client):
    resp = client.post("/admin/login", data={"admin_key": "wrong"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "error=invalid_key" in resp.headers.get("location", "")


def test_admin_dashboard_login_with_correct_key_sets_cookie(client):
    from app.core.config import get_settings
    s = get_settings()
    resp = client.post("/admin/login", data={"admin_key": s.effective_admin_key},
                       follow_redirects=False)
    assert resp.status_code == 303
    set_cookie = resp.headers.get("set-cookie", "")
    assert "tedw_admin=" in set_cookie
    assert "HttpOnly" in set_cookie


def test_admin_dashboard_metrics_with_cookie(client):
    from app.core.config import get_settings
    s = get_settings()
    client.post("/admin/login", data={"admin_key": s.effective_admin_key},
                follow_redirects=False)
    resp = client.get("/admin/api/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert "timestamp" in data
    assert "storage" in data
    assert "patterns" in data
    assert "b2" in data
    assert "sandbox" in data


def test_admin_dashboard_create_key_endpoint(client):
    from app.core.config import get_settings
    s = get_settings()
    client.post("/admin/login", data={"admin_key": s.effective_admin_key},
                follow_redirects=False)
    resp = client.post("/admin/api/keys", json={
        "label": "dashboard_test_client",
        "allowed_domains": ["example.com"],
        "rate_per_minute": 30,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "raw_token" in data
    assert data["raw_token"].startswith("tedw_uk_")
    assert "warning" in data


def test_admin_dashboard_render_endpoint_without_config(client):
    from app.core.config import get_settings
    s = get_settings()
    client.post("/admin/login", data={"admin_key": s.effective_admin_key},
                follow_redirects=False)
    resp = client.get("/admin/api/render/service")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is False or "error" in data


def test_admin_v1_b2_status_endpoint(client):
    """GET /v1/admin/b2/status returns B2 connection status."""
    from app.core.config import get_settings
    s = get_settings()
    resp = client.get("/v1/admin/b2/status", headers={
        "Authorization": f"Bearer {s.effective_admin_key}"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "configured" in data
    assert "limit_gb" in data


def test_admin_v1_sandbox_status_endpoint(client):
    """GET /v1/admin/sandbox/status returns the sandbox cron status."""
    from app.core.config import get_settings
    s = get_settings()
    resp = client.get("/v1/admin/sandbox/status", headers={
        "Authorization": f"Bearer {s.effective_admin_key}"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "running" in data
    assert "enabled" in data


# ---------------------------------------------------------------------------
# Security integration: bcrypt key + API access
# ---------------------------------------------------------------------------

def test_enduser_bcrypt_key_authorizes_predict_endpoint(client):
    """A registered bcrypt-hashed end-user key can call /v1/predict/sentiment."""
    from app.core.config import get_settings
    from app.core.bcrypt_auth import create_key
    s = get_settings()
    client.post("/admin/login", data={"admin_key": s.effective_admin_key},
                follow_redirects=False)
    resp = client.post("/admin/api/keys", json={"label": "test_eu"})
    raw_token = resp.json()["raw_token"]
    resp = client.post("/v1/predict/sentiment",
                       headers={"Authorization": f"Bearer {raw_token}"},
                       json={"text": "strong growth and upgrades"})
    assert resp.status_code == 200
    assert resp.json()["polarity"] > 0


def test_enduser_bcrypt_key_rejected_for_admin_routes(client):
    """An end-user key must NOT be able to call admin routes."""
    from app.core.config import get_settings
    from app.core.bcrypt_auth import create_key
    s = get_settings()
    client.post("/admin/login", data={"admin_key": s.effective_admin_key},
                follow_redirects=False)
    resp = client.post("/admin/api/keys", json={"label": "test_eu_admin_block"})
    raw_token = resp.json()["raw_token"]
    resp = client.get("/v1/admin/status",
                      headers={"Authorization": f"Bearer {raw_token}"})
    assert resp.status_code == 403
