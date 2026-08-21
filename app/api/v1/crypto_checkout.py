"""
app/api/v1/crypto_checkout.py — Pluggable crypto checkout endpoint.

This is a stubbed, pluggable endpoint. By default it returns "disabled"
unless CRYPTO_CHECKOUT_ENABLED=true. The intent is to support a future
end-user-tier upgrade flow where end-users pay via crypto to lift rate
limits.

Providers:
  * manual       — admin reviews and manually lifts limits (default).
  * nowpayments  — TODO: integrate NowPayments API.
  * coinbase     — TODO: integrate Coinbase Commerce.

Security:
  * End-user-accessible (no admin required), but read-only when disabled.
  * When a payment is recorded, only the user fingerprint (not raw key)
    is stored — consistent with the Zero-Logging Policy.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.security import AuthContext, get_auth_context

router = APIRouter()
logger = logging.getLogger("tony_edward.api.crypto")
logger.setLevel(logging.INFO)


class CheckoutRequest(BaseModel):
    """End-user request to start a crypto checkout session."""
    plan: str = Field("pro", description="pro | team | enterprise")
    currency: str = Field("usd")


class CheckoutResponse(BaseModel):
    ok: bool
    enabled: bool
    provider: str
    checkout_url: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    expires_at: Optional[float] = None
    error: Optional[str] = None


# Plan → USD price table (cents avoided for simplicity)
PLAN_PRICES_USD = {
    "pro": 29.0,
    "team": 99.0,
    "enterprise": 499.0,
}


@router.post("/checkout", response_model=CheckoutResponse)
async def start_checkout(
    req: CheckoutRequest,
    ctx: AuthContext = Depends(get_auth_context),
) -> CheckoutResponse:
    settings = get_settings()
    if not settings.crypto_checkout_enabled:
        return CheckoutResponse(
            ok=False,
            enabled=False,
            provider=settings.crypto_checkout_provider,
            error="crypto_checkout_disabled",
        )

    if req.plan not in PLAN_PRICES_USD:
        return CheckoutResponse(
            ok=False, enabled=True, provider=settings.crypto_checkout_provider,
            error="unknown_plan",
        )

    amount = PLAN_PRICES_USD[req.plan]

    if settings.crypto_checkout_provider == "manual":
        # Manual mode: return a placeholder; admin lifts limits after payment.
        return CheckoutResponse(
            ok=True,
            enabled=True,
            provider="manual",
            checkout_url=None,
            amount=amount,
            currency=req.currency,
            expires_at=None,
            error="contact_admin_to_complete_payment",
        )

    # TODO: nowpayments / coinbase-commerce integration
    return CheckoutResponse(
        ok=False,
        enabled=True,
        provider=settings.crypto_checkout_provider,
        error="provider_not_implemented",
    )


@router.get("/plans")
async def list_plans() -> dict:
    """Public endpoint — list available plans and prices."""
    return {
        "plans": [
            {"name": k, "price_usd": v, "features": _plan_features(k)}
            for k, v in PLAN_PRICES_USD.items()
        ],
    }


def _plan_features(plan: str) -> list[str]:
    if plan == "pro":
        return ["100 req/min", "BYO API key", "All scrapers"]
    if plan == "team":
        return ["500 req/min", "5 seats", "Pattern export", "Priority queue"]
    if plan == "enterprise":
        return ["Unlimited req/min", "Dedicated support", "On-prem deploy", "Custom scrapers"]
    return []


@router.get("/status")
async def checkout_status(ctx: AuthContext = Depends(get_auth_context)) -> dict:
    """Check the caller's current checkout / plan status."""
    settings = get_settings()
    return {
        "enabled": settings.crypto_checkout_enabled,
        "provider": settings.crypto_checkout_provider,
        "is_admin": ctx.is_admin,
        "current_role": ctx.role.value,
    }
