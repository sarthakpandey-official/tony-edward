"""
tests/test_system.py — Internal test suite for Tony-EDWARD.

Covers:
  - Config loading
  - Security role resolution
  - Zero-log middleware filter
  - Sentiment + velocity analysis
  - Predictive risk scoring
  - Algorithm synthesizer
  - Pattern learning (with a mock embedder)
  - Storage auto-purge logic
  - API routes (search, predict, admin, terminal info)
  - Health endpoint

Run: pytest tests/ -v
"""
import asyncio
import os
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio

# Make /app (i.e., the project root) importable
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Configure a temp storage dir BEFORE importing app modules
os.environ.setdefault("STORAGE_DIR", "/tmp/tony-edward-test")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("ADMIN_SECRET_KEY_64", "A" * 64)
os.environ.setdefault("SUPER_ADMIN_KEY", "tedw_sk_test_admin_key_1234567890")
os.environ.setdefault("JWT_SECRET", "test_jwt_secret_1234567890")
os.environ.setdefault("PRIMARY_LLM_API_URL", "https://api.openai.com/v1")
os.environ.setdefault("PRIMARY_LLM_API_KEY", "sk-test-fake-key-for-tests-only")

os.makedirs(os.environ["STORAGE_DIR"], exist_ok=True)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_config_loads():
    from app.core.config import get_settings, reload_settings
    s = reload_settings()
    assert s.port == 8000
    assert s.environment == "test"
    assert s.super_admin_key.startswith("tedw_sk_")
    assert s.scraper_min_delay_sec == 2.0
    assert s.scraper_max_delay_sec == 5.0
    assert s.auto_purge_storage_limit_gb == 20


def test_config_user_agent_pool():
    from app.core.config import get_settings
    s = get_settings()
    assert len(s.scraper_user_agents) >= 5
    assert all("Mozilla" in ua for ua in s.scraper_user_agents)


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

def test_role_resolution_admin():
    from app.core.security import resolve_role, Role
    from app.core.config import get_settings
    s = get_settings()
    # Admin key is the 64-char ADMIN_SECRET_KEY_64 (preferred over legacy super_admin_key)
    assert resolve_role(s, f"Bearer {s.effective_admin_key}") is Role.SUPER_ADMIN


def test_role_resolution_end_user_no_token():
    from app.core.security import resolve_role, Role
    from app.core.config import get_settings
    s = get_settings()
    assert resolve_role(s, None) is Role.END_USER


def test_role_resolution_end_user_wrong_token():
    from app.core.security import resolve_role, Role
    from app.core.config import get_settings
    s = get_settings()
    assert resolve_role(s, "Bearer wrong_token") is Role.END_USER


def test_admin_key_is_strict_64():
    from app.core.config import get_settings
    s = get_settings()
    assert s.admin_key_is_strict_64 is True
    assert len(s.admin_secret_key_64) == 64


def test_effective_admin_key_prefers_64char():
    from app.core.config import get_settings
    s = get_settings()
    assert s.effective_admin_key == s.admin_secret_key_64
    assert s.effective_admin_key != s.super_admin_key


def test_constant_time_eq():
    from app.core.security import _constant_time_eq
    assert _constant_time_eq("abc", "abc") is True
    assert _constant_time_eq("abc", "abd") is False


# ---------------------------------------------------------------------------
# Zero-Log Middleware
# ---------------------------------------------------------------------------

def test_zero_log_filter_installed():
    import logging
    from app.core.zero_log_middleware import install_zero_log_policy
    install_zero_log_policy()
    root = logging.getLogger()
    # Filter should be present
    assert any("ZeroLogFilter" in type(f).__name__ for f in root.filters)


def test_zero_log_filter_scrubs_payload():
    import logging
    from app.core.zero_log_middleware import install_zero_log_policy, _FORBIDDEN_LOG_KEYS
    install_zero_log_policy()
    # Forbidden keys should be in the set
    assert "query" in _FORBIDDEN_LOG_KEYS
    assert "body" in _FORBIDDEN_LOG_KEYS


# ---------------------------------------------------------------------------
# Sentiment
# ---------------------------------------------------------------------------

def test_sentiment_positive():
    from app.engine.sentiment_velocity import analyze
    r = analyze("The company announced record growth and a strong upgrade from analysts.")
    assert r.polarity > 0.1
    assert "growth" in r.pos_hits or "strong" in r.pos_hits or "upgrade" in r.pos_hits


def test_sentiment_negative():
    from app.engine.sentiment_velocity import analyze
    r = analyze("The stock plunged after a major fraud investigation and layoffs were announced.")
    assert r.polarity < -0.1


def test_sentiment_neutral():
    from app.engine.sentiment_velocity import analyze
    r = analyze("The meeting is scheduled for Tuesday afternoon.")
    assert abs(r.polarity) < 0.3


def test_sentiment_batch():
    from app.engine.sentiment_velocity import analyze_batch
    texts = [
        "Strong growth and upgrades.",
        "Plunge after fraud probe.",
        "The company released a press release today.",
    ]
    r = analyze_batch(texts)
    assert r.snippet_count == 3
    assert -1.0 <= r.polarity <= 1.0


def test_velocity():
    from app.engine.sentiment_velocity import velocity
    series = [
        (1.0, 0.2), (2.0, 0.3), (3.0, 0.5),
        (4.0, 0.4), (5.0, 0.6), (6.0, 0.8),
    ]
    v = velocity(series)
    assert len(v.velocities) == 5
    assert v.trend in ("up", "down", "flat")
    assert 0.0 <= v.burst_score <= 1.0


# ---------------------------------------------------------------------------
# Predictive Risk
# ---------------------------------------------------------------------------

def test_predictive_risk_returns_score():
    from app.engine.predictive_risk import from_texts, score
    signals = from_texts([
        "Strong growth and upgrades.",
        "Bullish rally accelerating.",
        "Profits exceeded expectations.",
    ])
    rs = score(signals, horizon_hours=24)
    assert 0.0 <= rs.overall <= 100.0
    assert "sentiment_trend" in rs.components
    assert "volatility" in rs.components
    assert rs.n_signals == 3


def test_predictive_risk_empty():
    from app.engine.predictive_risk import score
    rs = score([], horizon_hours=24)
    assert rs.overall == 0.0
    assert "no_signals" in rs.notes


# ---------------------------------------------------------------------------
# Algorithm Synthesizer
# ---------------------------------------------------------------------------

def test_engagement_formula_synthesis():
    from app.engine.algo_synthesizer import EngagementAlgorithm, apply_engagement
    algo = EngagementAlgorithm(
        host="example.com",
        app_type="html_static",
        engagement_formula="1.0 * likes + 0.8 * comments",
        decay_lambda=0.3,
        decay_formula="score * exp(-0.300 * age_days)",
        detected_signals=["likes", "comments"],
    )
    score = apply_engagement(algo, {"likes": 100, "comments": 10}, age_hours=24.0)
    assert score > 0
    # Apply decay: score should be less than non-decayed
    no_decay = apply_engagement(algo, {"likes": 100, "comments": 10}, age_hours=0.0)
    assert score < no_decay


def test_decay_lambda_estimation():
    from app.engine.algo_synthesizer import _estimate_decay_lambda, EngagementAlgorithm
    from app.scrapers.dynamic_app_adapter import AppProfile
    # RSS = fast decay
    p1 = AppProfile(fingerprint="x", host="x", app_type="rss_atom")
    assert _estimate_decay_lambda(p1, []) > 0.5
    # HTML static with upvotes = slow decay
    p2 = AppProfile(fingerprint="x", host="x", app_type="html_static")
    assert _estimate_decay_lambda(p2, ["upvotes"]) < 0.3


# ---------------------------------------------------------------------------
# Pattern Learning (with mocked embedder)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def mock_embed(monkeypatch):
    """Mock embed_text to avoid hitting the real LLM API during tests."""
    async def fake_embed(ctx, text, model="text-embedding-3-small", settings=None):
        # Deterministic fake embedding based on text hash
        import hashlib
        h = hashlib.sha256(text.encode()).digest()
        vec = [b / 255.0 for b in h[:128]]
        return True, vec, None

    from app.engine import model_router
    monkeypatch.setattr(model_router, "embed_text", fake_embed)
    # Also patch pattern_learning's import (it imports the function)
    from app.engine import pattern_learning
    monkeypatch.setattr(pattern_learning, "embed_text", fake_embed)


@pytest.mark.asyncio
async def test_pattern_observe_novel(mock_embed):
    from app.engine.pattern_learning import observe, PatternInput
    from app.core.config import get_settings
    from app.core.security import AuthContext, Role

    settings = get_settings()
    ctx = AuthContext(
        role=Role.SUPER_ADMIN,
        request_id="test123",
        byo_api_key=None,
        byo_api_url=None,
        is_admin=True,
    )

    # First observation should be novel (no neighbors)
    pat = PatternInput(
        task="test",
        source="test_source",
        text="first_pattern_test",
        label="positive",
    )
    result = await observe(ctx, pat, settings)
    assert result.observed is True
    assert result.reason == "novel_pattern_persisted"


@pytest.mark.asyncio
async def test_pattern_observe_duplicate(mock_embed):
    from app.engine.pattern_learning import observe, PatternInput
    from app.core.config import get_settings
    from app.core.security import AuthContext, Role

    settings = get_settings()
    ctx = AuthContext(
        role=Role.SUPER_ADMIN,
        request_id="test456",
        byo_api_key=None,
        byo_api_url=None,
        is_admin=True,
    )

    pat = PatternInput(
        task="test",
        source="test_source",
        text="duplicate_pattern_test_xyz",
        label="positive",
    )
    # First observation
    await observe(ctx, pat, settings)
    # Second identical observation should be rejected as duplicate
    result = await observe(ctx, pat, settings)
    assert result.observed is False
    assert "duplicate" in result.reason


def test_pattern_stats():
    from app.engine.pattern_learning import stats
    s = stats()
    assert "total_patterns" in s
    assert "by_source" in s


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_storage_size_calculation():
    from app.storage.pattern_db_cache import storage_dir_size_bytes, storage_dir_size_gb
    bytes_used = storage_dir_size_bytes()
    gb_used = storage_dir_size_gb()
    assert bytes_used >= 0
    assert gb_used >= 0


def test_storage_total_count():
    from app.storage.pattern_db_cache import total_count
    n = total_count()
    assert n >= 0


def test_storage_prune_low_novelty():
    from app.storage.pattern_db_cache import prune_low_novelty, total_count
    current = total_count()
    if current > 0:
        deleted = prune_low_novelty(current)  # no-op
        assert deleted == 0


# ---------------------------------------------------------------------------
# API Integration Tests (using FastAPI TestClient)
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """Each test gets a fresh app instance + fresh auto-purge + sandbox singletons.

    This avoids asyncio.Lock/Event bound to a closed event loop errors.
    """
    # Reset module-level singletons that may hold loop-bound state
    import app.storage.auto_purge as ap_module
    from app.engine.sandbox_cron import reset_sandbox_cron
    from app.core.bcrypt_auth import reset_cache
    from app.core.config import get_settings
    ap_module._auto_purge = None
    reset_sandbox_cron()
    settings = get_settings()
    # Clear the persisted bcrypt registry so tests start clean
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
    # Teardown: stop any spawned auto-purge loop
    if ap_module._auto_purge is not None:
        try:
            import asyncio
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


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "tony-edward"


def test_root_endpoint(client):
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert "service" in data
    assert "Tony-EDWARD" in data["service"]


def test_search_sources_public(client):
    resp = client.get("/v1/search/sources")
    assert resp.status_code == 200
    data = resp.json()
    assert "sources" in data
    assert len(data["sources"]) >= 2


def test_predict_sentiment(client):
    resp = client.post("/v1/predict/sentiment", json={
        "text": "The company announced strong growth and upgrades from analysts."
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "polarity" in data
    assert data["polarity"] > 0


def test_predict_velocity_endpoint(client):
    resp = client.post("/v1/predict/velocity", json={
        "series": [[1.0, 0.1], [2.0, 0.3], [3.0, 0.5], [4.0, 0.4]]
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "burst_score" in data
    assert "trend" in data


def test_predict_risk_endpoint(client):
    resp = client.post("/v1/predict/risk", json={
        "texts": ["Strong growth.", "Plunge after fraud probe.", "Bullish rally."],
        "source": "test",
        "horizon_hours": 24,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "overall" in data
    assert 0 <= data["overall"] <= 100


def test_admin_requires_super_admin(client):
    # No auth → 403
    resp = client.get("/v1/admin/status")
    assert resp.status_code == 403


def test_admin_with_super_admin_key(client):
    from app.core.config import get_settings
    s = get_settings()
    resp = client.get("/v1/admin/status", headers={
        "Authorization": f"Bearer {s.effective_admin_key}"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "storage" in data
    assert "patterns" in data
    assert "b2" in data
    assert data["storage"]["limit_gb"] == 20


def test_crypto_plans_public(client):
    resp = client.get("/v1/crypto/plans")
    assert resp.status_code == 200
    data = resp.json()
    assert "plans" in data
    assert len(data["plans"]) >= 3


def test_crypto_status_unauth(client):
    # End-user role should still work (no admin required)
    resp = client.get("/v1/crypto/status")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Terminal Exec (without WebSocket)
# ---------------------------------------------------------------------------

def test_terminal_manager_singleton():
    from app.core.terminal_exec import terminal_manager
    assert terminal_manager is not None


@pytest.mark.asyncio
async def test_terminal_session_lifecycle():
    """Spawn + close a terminal session."""
    from app.core.terminal_exec import TerminalManager
    mgr = TerminalManager()
    session = await mgr.acquire()
    assert session.session_id is not None
    assert session.master_fd >= 0
    # Send a command
    await session.write_stdin("echo hello_tony_edward\n")
    # Give it time
    await asyncio.sleep(0.5)
    # Collect output
    output = b""
    async for chunk in session.stream_output():
        output += chunk
        if b"hello_tony_edward" in output:
            break
        if len(output) > 1000:
            break
    await mgr.close_all()
    assert b"hello_tony_edward" in output


# ---------------------------------------------------------------------------
# Scraper routing
# ---------------------------------------------------------------------------

def test_router_url_validation():
    from app.scrapers.router import route_url
    # Invalid URL should return a ScrapeResult with error
    coro = route_url("not-a-url")
    # We need to actually run it
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(coro)
        assert result.ok is False
        assert "invalid_url" in (result.error or "")
    finally:
        loop.close()


def test_dynamic_app_adapter_registry():
    from app.scrapers.dynamic_app_adapter import registry_stats
    stats = registry_stats()
    assert "cached_sites" in stats
    assert "admin_overrides" in stats
