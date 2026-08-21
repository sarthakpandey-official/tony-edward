"""
app/core/config.py — Tony-EDWARD configuration loader (v3 — Backblaze B2).

Reads environment variables once at process startup and exposes a frozen
Settings object.

Storage architecture (v3):
  * Local SQLite — hot read-path for patterns (vectors + cosine scans)
  * Backblaze B2 (S3-compatible) — durable JSONL pattern storage (10GB cap)

Removed in v3:
  * Cloudflare R2 dual-cloud layer
  * Google BigQuery dual-cloud layer

LLM:
  * Primary key + fallback key with automatic failover (Requesty router).
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional


def _bool(val: Optional[str], default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on", "y"}


def _parse_tuple(val: Optional[str], default: tuple = ()) -> tuple:
    if not val:
        return default
    items = tuple(s.strip() for s in val.split(",") if s.strip())
    return items or default


@dataclass(frozen=True)
class Settings:
    # --- Process ---
    environment: str = "production"
    port: int = 8000
    host: str = "0.0.0.0"
    log_level: str = "INFO"

    # --- Sentry.io monitoring ---
    # Default DSN points to the user's Sentry project. Override via env var SENTRY_DSN.
    sentry_dsn: str = "https://bbbd4bcaa47ee5b0276c5372360ae8f3@o4511948811534336.ingest.us.sentry.io/4511948815794176"
    sentry_traces_sample_rate: float = 0.1

    # --- Security ---
    # `super_admin_key` is the legacy bearer used by v1 API + WebSocket terminal.
    # `admin_secret_key_64` is the strict 64-char key for the Web Admin UI.
    super_admin_key: str = ""
    admin_secret_key_64: str = ""
    admin_secret_key_min_len: int = 64
    jwt_secret: str = ""
    byo_api_header: str = "X-Tony-Edward-LLM-Key"
    byo_api_url_header: str = "X-Tony-Edward-LLM-Url"
    # End-user API key registry (bcrypt-hashed)
    enduser_api_keys_path: str = ""
    enduser_allowed_origins: tuple = ()
    enduser_allowed_domains: tuple = ()
    bcrypt_rounds: int = 12

    # Rate limits — end-user role
    enduser_rate_per_minute: int = 20
    enduser_rate_burst: int = 5
    enduser_max_concurrent: int = 4

    # --- LLM Routing ---
    primary_llm_api_url: str = ""
    primary_llm_api_key: str = ""
    primary_llm_api_key_fallback: str = ""    # auto-failover when primary is exhausted
    primary_llm_model: str = "gpt-4o-mini"
    default_timeout_llm: int = 30

    # --- Storage ---
    storage_dir: str = "/app/storage"
    auto_purge_storage_limit_gb: int = 20
    auto_purge_check_interval_sec: int = 300
    pattern_db_path: str = ""
    pattern_max_age_days: int = 90

    # --- Backblaze B2 (S3-compatible, single durable layer, 10GB cap) ---
    b2_application_key_id: str = ""
    b2_application_key: str = ""
    b2_bucket_name: str = ""
    b2_endpoint_url: str = ""
    b2_storage_limit_gb: int = 10
    b2_region: str = "us-east-005"

    # --- Scrapers ---
    scraper_min_delay_sec: float = 2.0
    scraper_max_delay_sec: float = 5.0
    scraper_request_timeout_sec: int = 30
    scraper_max_response_bytes: int = 5 * 1024 * 1024
    scraper_playwright_headless: bool = True
    scraper_user_agents: tuple = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    )

    # --- Render API integration ---
    render_api_key: str = ""
    render_service_id: str = ""
    render_api_base: str = "https://api.render.com/v1"

    # --- 30-day Sandbox Cron ---
    sandbox_cron_enabled: bool = True
    sandbox_cron_interval_days: int = 30
    sandbox_accuracy_threshold: float = 0.3
    sandbox_min_patterns_to_eval: int = 50

    # --- Pattern Learning ---
    pattern_min_novelty_cosine: float = 0.18
    pattern_max_records: int = 200_000

    # --- Crypto checkout (stubbed) ---
    crypto_checkout_enabled: bool = False
    crypto_checkout_provider: str = "manual"

    def __post_init__(self) -> None:
        if not self.pattern_db_path:
            object.__setattr__(self, "pattern_db_path",
                              os.path.join(self.storage_dir, "patterns.sqlite3"))
        if not self.enduser_api_keys_path:
            object.__setattr__(self, "enduser_api_keys_path",
                              os.path.join(self.storage_dir, "enduser_keys.json"))

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def effective_admin_key(self) -> str:
        """The key the dashboard checks. 64-char key wins if set."""
        return self.admin_secret_key_64 or self.super_admin_key

    @property
    def admin_key_is_strict_64(self) -> bool:
        return bool(self.admin_secret_key_64) and \
               len(self.admin_secret_key_64) >= self.admin_secret_key_min_len

    @property
    def b2_configured(self) -> bool:
        return bool(self.b2_application_key_id and self.b2_application_key
                    and self.b2_bucket_name and self.b2_endpoint_url)


def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    return val if val is not None else default


def _generate_super_admin_key() -> str:
    return f"tedw_sk_{secrets.token_urlsafe(32)}"


def _generate_64char_admin_key() -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(64))


def _generate_jwt_secret() -> str:
    return secrets.token_urlsafe(48)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    super_admin_key = _env("SUPER_ADMIN_KEY") or _generate_super_admin_key()
    admin_secret_key_64 = _env("ADMIN_SECRET_KEY_64") or _generate_64char_admin_key()
    jwt_secret = _env("JWT_SECRET") or _generate_jwt_secret()

    os.environ.setdefault("SUPER_ADMIN_KEY", super_admin_key)
    os.environ.setdefault("JWT_SECRET", jwt_secret)

    storage_dir = _env("STORAGE_DIR", "/app/storage")
    os.makedirs(storage_dir, exist_ok=True)

    return Settings(
        environment=_env("ENVIRONMENT", "production"),
        port=int(_env("PORT", "8000")),
        host=_env("HOST", "0.0.0.0"),
        log_level=_env("LOG_LEVEL", "INFO"),
        sentry_dsn=_env("SENTRY_DSN", "https://bbbd4bcaa47ee5b0276c5372360ae8f3@o4511948811534336.ingest.us.sentry.io/4511948815794176"),
        sentry_traces_sample_rate=float(_env("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
        super_admin_key=super_admin_key,
        admin_secret_key_64=admin_secret_key_64,
        admin_secret_key_min_len=int(_env("ADMIN_SECRET_KEY_MIN_LEN", "64")),
        jwt_secret=jwt_secret,
        byo_api_header=_env("BYO_API_HEADER", "X-Tony-Edward-LLM-Key"),
        byo_api_url_header=_env("BYO_API_URL_HEADER", "X-Tony-Edward-LLM-Url"),
        enduser_allowed_origins=_parse_tuple(_env("ENDUSER_ALLOWED_ORIGINS")),
        enduser_allowed_domains=_parse_tuple(_env("ENDUSER_ALLOWED_DOMAINS")),
        bcrypt_rounds=int(_env("BCRYPT_ROUNDS", "12")),
        enduser_rate_per_minute=int(_env("ENDUSER_RATE_PER_MINUTE", "20")),
        enduser_rate_burst=int(_env("ENDUSER_RATE_BURST", "5")),
        enduser_max_concurrent=int(_env("ENDUSER_MAX_CONCURRENT", "4")),
        primary_llm_api_url=_env("PRIMARY_LLM_API_URL"),
        primary_llm_api_key=_env("PRIMARY_LLM_API_KEY"),
        primary_llm_api_key_fallback=_env("PRIMARY_LLM_API_KEY_FALLBACK"),
        primary_llm_model=_env("PRIMARY_LLM_MODEL", "gpt-4o-mini"),
        default_timeout_llm=int(_env("DEFAULT_TIMEOUT_LLM", "30")),
        storage_dir=storage_dir,
        auto_purge_storage_limit_gb=int(_env("AUTO_PURGE_STORAGE_LIMIT_GB", "20")),
        auto_purge_check_interval_sec=int(_env("AUTO_PURGE_CHECK_INTERVAL_SEC", "300")),
        pattern_max_age_days=int(_env("PATTERN_MAX_AGE_DAYS", "90")),
        b2_application_key_id=_env("B2_APPLICATION_KEY_ID"),
        b2_application_key=_env("B2_APPLICATION_KEY"),
        b2_bucket_name=_env("B2_BUCKET_NAME"),
        b2_endpoint_url=_env("B2_ENDPOINT_URL"),
        b2_storage_limit_gb=int(_env("B2_STORAGE_LIMIT_GB", "10")),
        b2_region=_env("B2_REGION", "us-east-005"),
        scraper_min_delay_sec=float(_env("SCRAPER_MIN_DELAY_SEC", "2.0")),
        scraper_max_delay_sec=float(_env("SCRAPER_MAX_DELAY_SEC", "5.0")),
        scraper_request_timeout_sec=int(_env("SCRAPER_REQUEST_TIMEOUT_SEC", "30")),
        scraper_max_response_bytes=int(_env("SCRAPER_MAX_RESPONSE_BYTES", str(5 * 1024 * 1024))),
        scraper_playwright_headless=_bool(_env("SCRAPER_PLAYWRIGHT_HEADLESS", "true"), True),
        render_api_key=_env("RENDER_API_KEY"),
        render_service_id=_env("RENDER_SERVICE_ID"),
        render_api_base=_env("RENDER_API_BASE", "https://api.render.com/v1"),
        sandbox_cron_enabled=_bool(_env("SANDBOX_CRON_ENABLED", "true"), True),
        sandbox_cron_interval_days=int(_env("SANDBOX_CRON_INTERVAL_DAYS", "30")),
        sandbox_accuracy_threshold=float(_env("SANDBOX_ACCURACY_THRESHOLD", "0.3")),
        sandbox_min_patterns_to_eval=int(_env("SANDBOX_MIN_PATTERNS_TO_EVAL", "50")),
        pattern_min_novelty_cosine=float(_env("PATTERN_MIN_NOVELTY_COSINE", "0.18")),
        pattern_max_records=int(_env("PATTERN_MAX_RECORDS", "200000")),
        crypto_checkout_enabled=_bool(_env("CRYPTO_CHECKOUT_ENABLED", "false"), False),
        crypto_checkout_provider=_env("CRYPTO_CHECKOUT_PROVIDER", "manual"),
    )


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
