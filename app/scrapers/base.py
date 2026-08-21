"""
app/scrapers/base.py — Shared infrastructure for all upgraded scrapers.

Implements the hardening required by STEP 1:
  * Dynamic User-Agent rotation (one random UA per request, picked from the
    Settings.scraper_user_agents pool).
  * Randomized execution delays 2-5s (jittered between min/max from
    Settings) BEFORE every network call to avoid IP rate-limits and
    anti-bot bans.
  * Modern async via httpx.AsyncClient with a per-process client pool.
  * Optional Playwright fallback for sites that require JS execution
    (Twitter, Instagram, LinkedIn, login-walled Reddit variants).
  * Strict response size cap (Settings.scraper_max_response_bytes).
  * Zero-Logging: query strings are passed in, used in-memory, never
    persisted. The base client does not log URLs — only outcome codes.

Upgraded from Agent-Reach:
  Agent-Reach's channel classes only PROBE for external CLI availability
  (twitter-cli, yt-dlp, rdt-cli). They never make HTTP calls themselves.
  Tony-EDWARD adds direct httpx-based fetchers layered on top, so the
  system works on a fresh Render instance WITHOUT requiring the operator
  to pip-install half a dozen external CLIs.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.core.config import Settings, get_settings

logger = logging.getLogger("tony_edward.scraper")
logger.setLevel(logging.INFO)


@dataclass
class ScrapeResult:
    """Standardized scrape result across all channels."""
    channel: str
    url: str
    status: int
    ok: bool
    body_text: str = ""
    body_bytes: bytes = b""
    content_type: str = ""
    latency_ms: float = 0.0
    fetched_via: str = "httpx"     # httpx | playwright | cli
    error: Optional[str] = None
    meta: dict[str, Any] = None

    def __post_init__(self) -> None:
        if self.meta is None:
            self.meta = {}


# ---------------- shared client pool ----------------

_http_client: Optional[httpx.AsyncClient] = None
_http_client_lock = asyncio.Lock()


async def get_http_client(settings: Settings) -> httpx.AsyncClient:
    """Lazily create and cache a shared httpx.AsyncClient.

    Reusing one client gives us HTTP/2 keepalive across requests, which
    substantially reduces TLS handshake overhead against rate-limited
    hosts (Twitter, Reddit, etc.).
    """
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        return _http_client
    async with _http_client_lock:
        if _http_client is None or _http_client.is_closed:
            _http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.scraper_request_timeout_sec),
                follow_redirects=True,
                http2=True,
                limits=httpx.Limits(
                    max_connections=64,
                    max_keepalive_connections=16,
                    keepalive_expiry=120.0,
                ),
                # Trust no proxy env by default — Render has no outbound proxy.
                trust_env=False,
            )
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


# ---------------- helpers ----------------

def _random_ua(settings: Settings) -> str:
    return random.choice(settings.scraper_user_agents)


async def _jittered_delay(settings: Settings) -> None:
    """Sleep between min_delay and max_delay (inclusive) seconds."""
    delay = random.uniform(settings.scraper_min_delay_sec, settings.scraper_max_delay_sec)
    await asyncio.sleep(delay)


def _build_headers(settings: Settings, extra: Optional[dict] = None) -> dict:
    headers = {
        "User-Agent": _random_ua(settings),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.5",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "DNT": "1",
    }
    if extra:
        headers.update(extra)
    return headers


async def fetch(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
    channel: str = "unknown",
    require_js: bool = False,
    settings: Optional[Settings] = None,
) -> ScrapeResult:
    """Fetch a URL with UA rotation + jittered delay.

    Set `require_js=True` to fall back to Playwright if the httpx
    response looks like an anti-bot challenge page.
    """
    settings = settings or get_settings()
    start = time.perf_counter()
    await _jittered_delay(settings)

    client = await get_http_client(settings)
    h = _build_headers(settings, headers)

    try:
        resp = await client.request(
            method,
            url,
            headers=h,
            params=params,
            json=json_body,
        )
        body = resp.content
        ct = resp.headers.get("content-type", "")
        # Truncate if oversized
        truncated = False
        if len(body) > settings.scraper_max_response_bytes:
            body = body[: settings.scraper_max_response_bytes]
            truncated = True
        text = body.decode("utf-8", errors="replace")

        # Heuristic anti-bot detection
        if require_js and _looks_like_antibot(text, ct):
            return await _fetch_via_playwright(
                url, settings, channel, start, h
            )

        return ScrapeResult(
            channel=channel,
            url=url,
            status=resp.status_code,
            ok=resp.status_code < 400,
            body_text=text,
            body_bytes=body,
            content_type=ct,
            latency_ms=(time.perf_counter() - start) * 1000.0,
            fetched_via="httpx",
            meta={"truncated": truncated},
        )
    except httpx.HTTPError as exc:
        logger.warning("scraper_error channel=%s status=network_error err=%s", channel, type(exc).__name__)
        return ScrapeResult(
            channel=channel,
            url=url,
            status=0,
            ok=False,
            latency_ms=(time.perf_counter() - start) * 1000.0,
            error=f"{type(exc).__name__}: {exc}",
            fetched_via="httpx",
        )


_ANTIBOT_MARKERS = (
    "just a moment...", "checking your browser", "cf-browser-verification",
    "attention required! | cloudflare", "/cdn-cgi/challenge-platform/",
    "enable javascript and cookies to continue", "verify you are human",
)


def _looks_like_antibot(text: str, content_type: str) -> bool:
    sample = text[:8192].lower()
    return any(m in sample for m in _ANTIBOT_MARKERS)


# ---------------- Playwright fallback ----------------

_playwright_available: Optional[bool] = None


async def _check_playwright() -> bool:
    """Check if Playwright + Chromium are available (cached)."""
    global _playwright_available
    if _playwright_available is not None:
        return _playwright_available
    try:
        from playwright.async_api import async_playwright
        # Attempt to launch — if browsers aren't installed this will fail fast.
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            await browser.close()
        _playwright_available = True
    except Exception:
        _playwright_available = False
    return _playwright_available


async def _fetch_via_playwright(
    url: str,
    settings: Settings,
    channel: str,
    start: float,
    headers: dict,
) -> ScrapeResult:
    if not await _check_playwright():
        return ScrapeResult(
            channel=channel,
            url=url,
            status=0,
            ok=False,
            error="antibot_detected_and_playwright_unavailable",
            latency_ms=(time.perf_counter() - start) * 1000.0,
            fetched_via="playwright",
        )

    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=settings.scraper_playwright_headless)
            context = await browser.new_context(
                user_agent=headers.get("User-Agent", _random_ua(settings)),
                locale="en-US",
                viewport={"width": 1280, "height": 800},
            )
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=settings.scraper_request_timeout_sec * 1000)
            # Wait briefly for anti-bot challenges to resolve
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            html = await page.content()
            title = await page.title()
            await context.close()
            await browser.close()
            body_bytes = html.encode("utf-8")
            if len(body_bytes) > settings.scraper_max_response_bytes:
                body_bytes = body_bytes[: settings.scraper_max_response_bytes]
            return ScrapeResult(
                channel=channel,
                url=url,
                status=200,
                ok=True,
                body_text=html,
                body_bytes=body_bytes,
                content_type="text/html",
                latency_ms=(time.perf_counter() - start) * 1000.0,
                fetched_via="playwright",
                meta={"title": title, "antibot_bypassed": True},
            )
    except Exception as exc:
        return ScrapeResult(
            channel=channel,
            url=url,
            status=0,
            ok=False,
            error=f"playwright_error: {exc}",
            latency_ms=(time.perf_counter() - start) * 1000.0,
            fetched_via="playwright",
        )


# ---------------- shared parsing utilities ----------------

def truncate_text(text: str, max_chars: int = 50_000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated]"
