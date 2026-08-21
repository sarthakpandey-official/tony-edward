"""
app/engine/model_router.py — Model-agnostic LLM router.

Routes LLM calls to the right backend based on:
  * AuthContext (admin → primary system LLM; end_user → BYO key + URL).
  * Task type (sentiment, summary, classify, embed, predict).
  * Provider auto-detection from URL (OpenAI, Anthropic, Groq, Mistral,
    OpenRouter, Together, Ollama, vLLM, LM Studio).

  * Zero-Logging: prompts/completions are NEVER logged, NEVER persisted.
  * BYO mode: end-user's API key is held only for the duration of the
    request; it is never written to disk, never cached beyond request
    lifetime.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.core.config import Settings, get_settings
from app.core.security import AuthContext, Role

logger = logging.getLogger("tony_edward.llm")
logger.setLevel(logging.INFO)


@dataclass
class LLMRequest:
    messages: list[dict[str, str]]
    model: Optional[str] = None
    temperature: float = 0.3
    max_tokens: int = 1000
    task: str = "chat"     # chat | summarize | classify | predict | embed


@dataclass
class LLMResponse:
    ok: bool
    text: str = ""
    model: str = ""
    provider: str = ""
    usage: dict[str, int] = None
    error: Optional[str] = None
    latency_ms: float = 0.0

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = {}


def detect_provider(api_url: str) -> str:
    """Infer provider from the API URL."""
    if not api_url:
        return "openai"
    u = api_url.lower()
    if "openai.com" in u or "api.openai.com" in u:
        return "openai"
    if "anthropic.com" in u:
        return "anthropic"
    if "groq.com" in u:
        return "groq"
    if "mistral.ai" in u:
        return "mistral"
    if "openrouter.ai" in u:
        return "openrouter"
    if "together.xyz" in u:
        return "together"
    if "/v1/chat/completions" in u:
        return "openai_compat"
    return "openai_compat"


def _resolve_endpoint(api_url: str, task: str) -> str:
    """Build the canonical endpoint URL for the given task."""
    if not api_url:
        api_url = "https://api.openai.com/v1"
    base = api_url.rstrip("/")
    if task == "embed":
        return f"{base}/embeddings"
    return f"{base}/chat/completions"


def _resolve_model(api_url: str, requested: Optional[str], task: str) -> str:
    if requested:
        return requested
    provider = detect_provider(api_url)
    defaults = {
        "openai": "gpt-4o-mini",
        "anthropic": "claude-3-5-haiku-latest",
        "groq": "llama-3.1-8b-instant",
        "mistral": "mistral-small-latest",
        "openrouter": "openai/gpt-4o-mini",
        "together": "meta-llama/Llama-3-8b-chat-hf",
        "openai_compat": "local-model",
    }
    return defaults.get(provider, "gpt-4o-mini")


def _build_payload_and_headers(
    api_key: str,
    model: str,
    req: LLMRequest,
    provider: str,
    api_url: str,
) -> tuple[dict, dict, str]:
    """Build (payload, headers, endpoint) for a single attempt."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": req.messages,
        "temperature": req.temperature,
    }
    if req.task != "embed":
        payload["max_tokens"] = req.max_tokens

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    endpoint = _resolve_endpoint(api_url, req.task)
    if provider == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        payload = {
            "model": model,
            "messages": [{"role": m["role"], "content": m["content"]} for m in req.messages],
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
        }
        endpoint = f"{api_url.rstrip('/')}/messages"
    return payload, headers, endpoint


async def _attempt_llm_call(
    api_key: str,
    api_url: str,
    model: str,
    req: LLMRequest,
    provider: str,
    settings: Settings,
) -> tuple[LLMResponse, int]:
    """One attempt. Returns (response, http_status_code)."""
    import time
    payload, headers, endpoint = _build_payload_and_headers(api_key, model, req, provider, api_url)
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=settings.default_timeout_llm) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
        latency_ms = (time.perf_counter() - start) * 1000.0
        if resp.status_code >= 400:
            return (
                LLMResponse(
                    ok=False,
                    error=f"http_{resp.status_code}",
                    provider=provider,
                    model=model,
                    latency_ms=latency_ms,
                ),
                resp.status_code,
            )
        data = resp.json()
        if provider == "anthropic":
            text_parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
            return (
                LLMResponse(
                    ok=True,
                    text="".join(text_parts),
                    model=data.get("model", model),
                    provider=provider,
                    usage={
                        "input": data.get("usage", {}).get("input_tokens", 0),
                        "output": data.get("usage", {}).get("output_tokens", 0),
                    },
                    latency_ms=latency_ms,
                ),
                resp.status_code,
            )
        # OpenAI-compatible
        return (
            LLMResponse(
                ok=True,
                text=data.get("choices", [{}])[0].get("message", {}).get("content", ""),
                model=data.get("model", model),
                provider=provider,
                usage=data.get("usage", {}),
                latency_ms=latency_ms,
            ),
            resp.status_code,
        )
    except httpx.HTTPError as exc:
        return (
            LLMResponse(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                provider=provider,
                model=model,
            ),
            0,
        )


# Status codes that should trigger an automatic fallback retry
# 401 = unauthorized (key revoked)
# 403 = forbidden (key invalid)
# 429 = rate limit (key exhausted)
# 402 = payment required (billing issue)
_FAILOVER_STATUSES = {401, 402, 403, 429}


async def call_llm(
    ctx: AuthContext,
    req: LLMRequest,
    settings: Optional[Settings] = None,
) -> LLMResponse:
    """Make an LLM call with automatic failover for admin role.

    For admin role:
      * Try PRIMARY_LLM_API_KEY first.
      * If 401/402/403/429, automatically retry with
        PRIMARY_LLM_API_KEY_FALLBACK (if configured).
      * Both keys share the same URL + model (Requesty router pattern).

    For end-user role:
      * Use their BYO key only (no failover).
    """
    settings = settings or get_settings()
    api_url = ctx.llm_api_url or settings.primary_llm_api_url
    if not api_url:
        api_url = "https://api.openai.com/v1"
    model = _resolve_model(api_url, req.model, req.task)
    provider = detect_provider(api_url)

    if ctx.is_admin:
        # Admin: try primary, failover to fallback on 401/402/403/429
        primary_key = settings.primary_llm_api_key
        fallback_key = settings.primary_llm_api_key_fallback

        if not primary_key and not fallback_key:
            return LLMResponse(ok=False, error="no_llm_api_key_configured",
                               provider=provider, model=model)

        keys_to_try = [k for k in [primary_key, fallback_key] if k]
    else:
        # End-user: BYO key only
        byo = ctx.byo_api_key
        if not byo:
            return LLMResponse(ok=False, error="end_user_must_supply_byo_api_key",
                               provider=provider, model=model)
        keys_to_try = [byo]

    last_response: Optional[LLMResponse] = None
    for i, key in enumerate(keys_to_try):
        response, status_code = await _attempt_llm_call(
            key, api_url, model, req, provider, settings
        )
        last_response = response
        # If success or non-failover error, return immediately
        if response.ok:
            return response
        if status_code not in _FAILOVER_STATUSES:
            return response
        # If this was the last key, return the failure
        if i == len(keys_to_try) - 1:
            logger.warning(
                "llm_all_keys_exhausted attempts=%d last_error=%s",
                len(keys_to_try), response.error
            )
            return response
        # Otherwise log + try the next key
        logger.info(
            "llm_failover attempt=%d status=%d → retrying_with_next_key",
            i + 1, status_code
        )
    return last_response or LLMResponse(ok=False, error="no_attempts_made",
                                         provider=provider, model=model)


async def embed_text(
    ctx: AuthContext,
    text: str,
    model: str = "text-embedding-3-small",
    settings: Optional[Settings] = None,
) -> tuple[bool, list[float], Optional[str]]:
    """Embed text using the role-appropriate LLM. Returns (ok, vector, error).

    Also supports admin failover: primary → fallback key on 401/402/403/429.
    """
    settings = settings or get_settings()
    api_url = ctx.llm_api_url or settings.primary_llm_api_url or "https://api.openai.com/v1"

    if ctx.is_admin:
        keys_to_try = [k for k in [settings.primary_llm_api_key,
                                    settings.primary_llm_api_key_fallback] if k]
    else:
        byo = ctx.byo_api_key
        if not byo:
            return False, [], "end_user_must_supply_byo_api_key"
        keys_to_try = [byo]

    if not keys_to_try:
        return False, [], "no_llm_api_key_configured"

    endpoint = _resolve_endpoint(api_url, "embed")
    last_err: Optional[str] = None
    for i, key in enumerate(keys_to_try):
        try:
            async with httpx.AsyncClient(timeout=settings.default_timeout_llm) as client:
                resp = await client.post(
                    endpoint,
                    json={"input": text, "model": model},
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                )
            if resp.status_code in _FAILOVER_STATUSES and i < len(keys_to_try) - 1:
                logger.info("embed_failover attempt=%d status=%d", i + 1, resp.status_code)
                last_err = f"http_{resp.status_code}"
                continue
            if resp.status_code >= 400:
                return False, [], f"http_{resp.status_code}"
            data = resp.json()
            vec = data.get("data", [{}])[0].get("embedding", [])
            return True, vec, None
        except httpx.HTTPError as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            continue
    return False, [], last_err or "all_keys_exhausted"
