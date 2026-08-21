"""
app/api/v1/search.py — Search & URL scraping endpoints.

Routes:
  POST /v1/search          — search across sources (default: Google News)
  POST /v1/search/url      — fetch a specific URL via the router
  POST /v1/search/adaptive — force the dynamic app adapter
  GET  /v1/search/sources  — list available sources

Auth:
  * Both roles accepted.
  * End-users are rate-limited (see app/core/security.py).
  * End-users MUST pass BYO API key in headers (for any LLM-touching
    downstream processing).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.security import AuthContext, get_auth_context
from app.scrapers.router import route_search, route_url, route_adaptive
from app.engine.pattern_learning import observe, PatternInput

router = APIRouter()
logger = logging.getLogger("tony_edward.api.search")
logger.setLevel(logging.INFO)


# ---------------- models ----------------

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    source: Optional[str] = Field("news", description="news | reddit | twitter")
    limit: int = Field(20, ge=1, le=100)


class URLRequest(BaseModel):
    url: str = Field(..., min_length=10, max_length=2000)


class AdaptiveRequest(BaseModel):
    url: str = Field(..., min_length=10, max_length=2000)


class SearchResponse(BaseModel):
    ok: bool
    channel: str
    url: str
    status: int
    text_preview: str
    fetched_via: str
    latency_ms: float
    meta: dict
    error: Optional[str] = None
    pattern_observed: Optional[dict] = None


# ---------------- handlers ----------------

@router.post("", response_model=SearchResponse)
async def search(req: SearchRequest, ctx: AuthContext = Depends(get_auth_context)) -> SearchResponse:
    """Search across a named source."""
    result = await route_search(req.query, source=req.source, limit=req.limit)
    pattern_obs = None
    if result.ok and result.body_text:
        # Observe pattern (uses abstracted text, never raw query)
        try:
            po = await observe(
                ctx,
                PatternInput(
                    task="search",
                    source=result.channel,
                    text=f"search source={result.channel} fetched_via={result.fetched_via}",  # abstracted
                    label=None,
                ),
            )
            pattern_obs = {
                "observed": po.observed,
                "novelty_score": round(po.novelty_score, 4),
                "nearest_similarity": round(po.nearest_similarity, 4),
                "reason": po.reason,
            }
        except Exception:
            pass  # Pattern observation is best-effort; never fail the search

    return SearchResponse(
        ok=result.ok,
        channel=result.channel,
        url=result.url,
        status=result.status,
        text_preview=result.body_text[:500],
        fetched_via=result.fetched_via,
        latency_ms=round(result.latency_ms, 2),
        meta=result.meta,
        error=result.error,
        pattern_observed=pattern_obs,
    )


@router.post("/url", response_model=SearchResponse)
async def search_url(req: URLRequest, ctx: AuthContext = Depends(get_auth_context)) -> SearchResponse:
    """Fetch a specific URL via the router."""
    result = await route_url(req.url)
    pattern_obs = None
    if result.ok and result.body_text:
        try:
            po = await observe(
                ctx,
                PatternInput(
                    task="search_url",
                    source=result.channel,
                    text=f"scrape channel={result.channel} url_host={req.url.split('/')[2] if '/' in req.url else 'unknown'}",
                    label=None,
                ),
            )
            pattern_obs = {
                "observed": po.observed,
                "novelty_score": round(po.novelty_score, 4),
                "reason": po.reason,
            }
        except Exception:
            pass
    return SearchResponse(
        ok=result.ok,
        channel=result.channel,
        url=result.url,
        status=result.status,
        text_preview=result.body_text[:500],
        fetched_via=result.fetched_via,
        latency_ms=round(result.latency_ms, 2),
        meta=result.meta,
        error=result.error,
        pattern_observed=pattern_obs,
    )


@router.post("/adaptive", response_model=SearchResponse)
async def search_adaptive(req: AdaptiveRequest, ctx: AuthContext = Depends(get_auth_context)) -> SearchResponse:
    """Force the dynamic app adapter on a URL (skip known-channel fast path)."""
    result = await route_adaptive(req.url)
    return SearchResponse(
        ok=result.ok,
        channel=result.channel,
        url=result.url,
        status=result.status,
        text_preview=result.body_text[:500],
        fetched_via=result.fetched_via,
        latency_ms=round(result.latency_ms, 2),
        meta=result.meta,
        error=result.error,
    )


@router.get("/sources")
async def list_sources() -> dict:
    """List available search sources. Public endpoint — no auth."""
    return {
        "sources": [
            {"name": "news", "description": "Google News RSS search"},
            {"name": "reddit", "description": "Subreddit top posts (JSON API)"},
            {"name": "twitter", "description": "User timeline via nitter mirror"},
        ],
        "channels": ["twitter", "reddit", "youtube", "news", "web", "adapter"],
    }
