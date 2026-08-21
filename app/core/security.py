"""
app/core/security.py — Dual-role authorization for Tony-EDWARD.

Two roles:
  * Role.SUPER_ADMIN  — company operator. Uses ADMIN_SECRET_KEY_64 (or
                        legacy SUPER_ADMIN_KEY fallback). Unrestricted
                        velocity, full Render terminal access, no rate
                        limits, runs on primary system LLM credentials.
  * Role.END_USER     — third-party caller. Uses bcrypt-hashed API key
                        from the registry (see bcrypt_auth.py). Strict
                        token-bucket throttling, domain-binding, origin
                        checks, NO access to terminal/admin/DB routes.

Zero-Logging Policy:
  * request_id is a random per-call nonce — NOT derived from user data.
  * The security logger records ONLY role outcomes (admin_allowed /
    denied / enduser_allowed), never the underlying payload or query.
  * BYO LLM API keys are SHA-256 fingerprinted; raw keys never persisted.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from fastapi import Header, HTTPException, Request, status

from app.core.config import Settings, get_settings


_thread_local = threading.local()


class Role(str, Enum):
    SUPER_ADMIN = "super_admin"
    END_USER = "end_user"


@dataclass(frozen=True)
class AuthContext:
    role: Role
    request_id: str
    byo_api_key: Optional[str]
    byo_api_url: Optional[str]
    is_admin: bool
    enduser_key_id: Optional[str] = None

    @property
    def llm_api_key(self) -> str:
        if self.is_admin:
            return get_settings().primary_llm_api_key
        return self.byo_api_key or ""

    @property
    def llm_api_url(self) -> str:
        if self.is_admin:
            return get_settings().primary_llm_api_url
        return self.byo_api_url or get_settings().primary_llm_api_url


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def resolve_role(
    settings: Settings,
    authorization: Optional[str],
    origin: Optional[str] = None,
    host: Optional[str] = None,
) -> Role:
    """Resolve role from bearer token.

    Admin: constant-time compare against settings.effective_admin_key
          (64-char ADMIN_SECRET_KEY_64 when configured, else legacy
          super_admin_key).
    End-user: bcrypt-verify against the registry. Domain-binding + origin
              checks enforced inside verify_token.
    """
    token = _extract_bearer(authorization)
    if not token:
        return Role.END_USER
    # Admin check first (constant-time)
    if _constant_time_eq(token, settings.effective_admin_key):
        return Role.SUPER_ADMIN
    # End-user check — bcrypt
    from app.core.bcrypt_auth import verify_token
    entry = verify_token(token, origin=origin, host=host, settings=settings)
    if entry is not None:
        _thread_local.enduser_entry = entry
        return Role.END_USER
    return Role.END_USER


_user_buckets: dict[str, dict] = {}


def _user_fingerprint(byo_key: Optional[str], byo_url: Optional[str], client_host: str) -> str:
    material = "|".join([byo_key or "", byo_url or "", client_host])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _check_rate_limit(settings: Settings, fp: str, rate_per_minute: int) -> None:
    """Token-bucket per user fingerprint. Raises HTTP 429 when empty."""
    now = time.time()
    bucket = _user_buckets.get(fp)
    if bucket is None:
        bucket = {"tokens": float(rate_per_minute), "last": now, "rate": rate_per_minute}
        _user_buckets[fp] = bucket
    if bucket.get("rate") != rate_per_minute:
        bucket["rate"] = rate_per_minute
        bucket["tokens"] = min(float(rate_per_minute), bucket["tokens"])

    elapsed = now - bucket["last"]
    refill = elapsed * (rate_per_minute / 60.0)
    bucket["tokens"] = min(float(rate_per_minute), bucket["tokens"] + refill)
    bucket["last"] = now

    if bucket["tokens"] < 1.0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limit_exceeded",
            headers={"Retry-After": str(int(60 - elapsed))},
        )
    bucket["tokens"] -= 1.0


def build_auth_context(
    settings: Settings,
    request: Request,
    authorization: Optional[str],
    byo_api_key: Optional[str],
    byo_api_url: Optional[str],
) -> AuthContext:
    """Build AuthContext, applying role rules + end-user rate limit."""
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    role = resolve_role(settings, authorization, origin=origin, host=host)
    request_id = secrets.token_hex(12)

    if role is Role.SUPER_ADMIN:
        return AuthContext(
            role=role,
            request_id=request_id,
            byo_api_key=None,
            byo_api_url=None,
            is_admin=True,
        )

    enduser_entry = getattr(_thread_local, "enduser_entry", None)
    enduser_key_id = None
    rate_per_minute = settings.enduser_rate_per_minute
    if enduser_entry is not None:
        enduser_key_id = enduser_entry.key_id
        rate_per_minute = enduser_entry.rate_per_minute or settings.enduser_rate_per_minute
        _thread_local.enduser_entry = None

    client_host = (request.client.host if request.client else "unknown") or "unknown"
    fp = enduser_key_id or _user_fingerprint(byo_api_key, byo_api_url, client_host)
    _check_rate_limit(settings, fp, rate_per_minute)

    return AuthContext(
        role=role,
        request_id=request_id,
        byo_api_key=byo_api_key,
        byo_api_url=byo_api_url,
        is_admin=False,
        enduser_key_id=enduser_key_id,
    )


def get_auth_context(
    request: Request,
    authorization: Optional[str] = Header(None),
    byo_api_key: Optional[str] = Header(None, alias="X-Tony-Edward-LLM-Key"),
    byo_api_url: Optional[str] = Header(None, alias="X-Tony-Edward-LLM-Url"),
) -> AuthContext:
    return build_auth_context(
        get_settings(),
        request,
        authorization,
        byo_api_key,
        byo_api_url,
    )
