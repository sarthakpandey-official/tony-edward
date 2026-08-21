"""
app/scrapers/channels/web.py — Generic web scraper.

Uses Jina Reader (https://r.jina.ai/) as the primary backend, with a
direct httpx fetch + Playwright fallback. Jina Reader returns clean
Markdown for any URL, no API key required.

Upgraded from Agent-Reach:
  Agent-Reach's WebChannel only supports Jina Reader. We keep that path
  but add: (a) Playwright fallback for JS-heavy sites, (b) UA rotation,
  (c) jittered delays, (d) response size cap.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from app.scrapers.base import ScrapeResult, fetch, truncate_text


JINA_READER_URL = "https://r.jina.ai/"


async def scrape_url(url: str) -> ScrapeResult:
    """Read any URL via Jina Reader, then fall back to Playwright."""
    # Validate URL first
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return ScrapeResult(
            channel="web",
            url=url,
            status=400,
            ok=False,
            error="invalid_url",
        )
    if p.scheme not in ("http", "https"):
        return ScrapeResult(
            channel="web",
            url=url,
            status=400,
            ok=False,
            error="unsupported_scheme",
        )

    # 1. Jina Reader
    jina_url = JINA_READER_URL + url
    res = await fetch(
        jina_url,
        channel="web",
        headers={
            "Accept": "text/plain",
            "X-Return-Format": "markdown",
        },
    )
    if res.ok and not _is_antibot(res.body_text):
        res.meta["backend"] = "jina_reader"
        return res

    # 2. Direct fetch
    direct = await fetch(url, channel="web")
    if direct.ok and direct.content_type.startswith("text/"):
        text = _strip_html(direct.body_text)
        direct.body_text = truncate_text(text)
        direct.meta["backend"] = "direct_httpx"
        return direct

    # 3. Playwright fallback for JS-heavy sites
    if not direct.ok or direct.status in (403, 429, 503):
        pw = await fetch(url, channel="web", require_js=True)
        if pw.ok:
            pw.meta["backend"] = "playwright"
            return pw

    # Last resort — return whatever Jina gave us, even if it was an antibot
    res.meta["backend"] = "jina_reader_fallback"
    return res


_ANTIBOT_MARKERS = (
    "warning: requiring captcha",
    "title: just a moment...",
    "title: attention required! | cloudflare",
    "## performing security verification",
)


def _is_antibot(text: str) -> bool:
    sample = text[:4096].lower()
    return any(m in sample for m in _ANTIBOT_MARKERS)


def _strip_html(html: str) -> str:
    # Strip scripts and styles
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Strip tags
    html = re.sub(r'<[^>]+>', ' ', html)
    html = re.sub(r'\s+', ' ', html).strip()
    return html


def can_handle(url: str) -> bool:
    """Web channel is the universal fallback — always returns True."""
    return True
