"""
app/core/bcrypt_auth.py — End-user API key registry (bcrypt-hashed).

End-user API keys are presented via `Authorization: Bearer <token>`. We
NEVER store the raw token. We store only:

    {
        "key_id":         "uk_<8 hex chars>",
        "bcrypt_hash":    "$2b$12$...",
        "allowed_domains": ["example.com"],
        "allowed_origins": ["https://app.example.com"],
        "rate_per_minute": 60,
        "created_at":     1234567890,
        "last_used_at":   0,
        "use_count":      0,
        "active":         true,
        "label":          "client-name"
    }

Registry persisted as JSON at Settings.enduser_api_keys_path.

Domain-binding + Origin checks defend against CSRF-style token theft.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from app.core.config import Settings, get_settings

logger = logging.getLogger("tony_edward.bcrypt_auth")
logger.setLevel(logging.INFO)

try:
    import bcrypt
    _HAS_BCRYPT = True
except ImportError:
    _HAS_BCRYPT = False
    logger.warning("bcrypt_not_installed — end-user auth disabled")


# Tier system: 'limited' applies default rate limits + sandbox restrictions.
# 'unlimited' = no rate limits, no sandbox — full unrestricted access (admin-trusted).
VALID_TIERS = ("limited", "unlimited")


@dataclass
class EndUserKey:
    key_id: str
    bcrypt_hash: str
    allowed_domains: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)
    rate_per_minute: int = 20
    created_at: float = 0.0
    last_used_at: float = 0.0
    use_count: int = 0
    active: bool = True
    label: str = ""
    tier: str = "limited"    # limited | unlimited

    @property
    def is_unlimited(self) -> bool:
        return self.tier == "unlimited"


_registry_lock = threading.Lock()
_registry: Optional[dict[str, EndUserKey]] = None


def _load_registry(settings: Settings) -> dict[str, EndUserKey]:
    global _registry
    if _registry is not None:
        return _registry
    if not os.path.exists(settings.enduser_api_keys_path):
        _registry = {}
        return _registry
    try:
        with open(settings.enduser_api_keys_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _registry = {
            item["key_id"]: EndUserKey(**item)
            for item in data.get("keys", [])
        }
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        logger.warning("enduser_keys_load_failed err=%s", type(exc).__name__)
        _registry = {}
    return _registry


def _persist_registry(settings: Settings) -> None:
    if _registry is None:
        return
    os.makedirs(os.path.dirname(settings.enduser_api_keys_path), exist_ok=True)
    data = {"keys": [asdict(k) for k in _registry.values()]}
    tmp = settings.enduser_api_keys_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, settings.enduser_api_keys_path)


def _key_id_for_token(token: str) -> str:
    return "uk_" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def create_key(
    label: str = "",
    allowed_domains: Optional[list[str]] = None,
    allowed_origins: Optional[list[str]] = None,
    rate_per_minute: int = 20,
    tier: str = "limited",
    settings: Optional[Settings] = None,
) -> tuple[str, EndUserKey]:
    """Generate a new end-user API key. Returns (raw_token, EndUserKey).

    The raw_token is shown to the operator ONCE for transmission to the
    end-user. We do NOT persist it — only the bcrypt hash.

    tier='limited'   → default rate limits apply (rate_per_minute)
    tier='unlimited' → no rate limits, no sandbox restrictions (full access)
    """
    settings = settings or get_settings()
    if not _HAS_BCRYPT:
        raise RuntimeError("bcrypt_not_installed")
    if tier not in VALID_TIERS:
        raise ValueError(f"invalid_tier: {tier}; must be one of {VALID_TIERS}")

    raw_token = "tedw_uk_" + secrets.token_urlsafe(32)
    key_id = _key_id_for_token(raw_token)
    salt = bcrypt.gensalt(rounds=settings.bcrypt_rounds)
    hashed = bcrypt.hashpw(raw_token.encode("utf-8"), salt).decode("ascii")

    entry = EndUserKey(
        key_id=key_id,
        bcrypt_hash=hashed,
        allowed_domains=list(allowed_domains or []),
        allowed_origins=list(allowed_origins or []),
        rate_per_minute=rate_per_minute,
        created_at=time.time(),
        label=label,
        tier=tier,
    )
    with _registry_lock:
        reg = _load_registry(settings)
        reg[key_id] = entry
        _persist_registry(settings)
    logger.info("enduser_key_created key_id=%s label=%s", key_id, label)
    return raw_token, entry


def revoke_key(key_id: str, settings: Optional[Settings] = None) -> bool:
    settings = settings or get_settings()
    with _registry_lock:
        reg = _load_registry(settings)
        if key_id not in reg:
            return False
        del reg[key_id]
        _persist_registry(settings)
        logger.info("enduser_key_revoked key_id=%s", key_id)
        return True


def list_keys(settings: Optional[Settings] = None) -> list[dict]:
    """Return all keys WITHOUT bcrypt hashes. For the admin dashboard."""
    settings = settings or get_settings()
    reg = _load_registry(settings)
    return [
        {
            "key_id": k.key_id,
            "allowed_domains": k.allowed_domains,
            "allowed_origins": k.allowed_origins,
            "rate_per_minute": k.rate_per_minute,
            "created_at": k.created_at,
            "last_used_at": k.last_used_at,
            "use_count": k.use_count,
            "active": k.active,
            "label": k.label,
            "tier": k.tier,
            "is_unlimited": k.is_unlimited,
        }
        for k in reg.values()
    ]


def verify_token(
    token: str,
    origin: Optional[str],
    host: Optional[str],
    settings: Optional[Settings] = None,
) -> Optional[EndUserKey]:
    """Verify a presented bearer token.

    Returns the EndUserKey if valid (bcrypt match + domain/origin checks).
    Returns None otherwise. Updates last_used_at + use_count on success.
    """
    settings = settings or get_settings()
    if not _HAS_BCRYPT:
        return None

    reg = _load_registry(settings)
    key_id = _key_id_for_token(token)
    entry = reg.get(key_id)
    if entry is None or not entry.active:
        return None

    try:
        if not bcrypt.checkpw(token.encode("utf-8"), entry.bcrypt_hash.encode("ascii")):
            return None
    except (ValueError, TypeError):
        return None

    if entry.allowed_domains and host:
        if not any(host.endswith(d) for d in entry.allowed_domains):
            logger.info("enduser_key_domain_rejected key_id=%s host=%s", key_id, host)
            return None

    if entry.allowed_origins and origin:
        if origin not in entry.allowed_origins:
            logger.info("enduser_key_origin_rejected key_id=%s origin=%s", key_id, origin)
            return None

    with _registry_lock:
        entry.last_used_at = time.time()
        entry.use_count += 1
        try:
            _persist_registry(settings)
        except OSError:
            pass
    return entry


def registry_stats(settings: Optional[Settings] = None) -> dict:
    settings = settings or get_settings()
    reg = _load_registry(settings)
    return {
        "total_keys": len(reg),
        "active_keys": sum(1 for k in reg.values() if k.active),
        "bcrypt_rounds": settings.bcrypt_rounds,
        "bcrypt_available": _HAS_BCRYPT,
    }


def reset_cache() -> None:
    global _registry
    _registry = None
