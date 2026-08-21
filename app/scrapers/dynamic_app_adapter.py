"""
app/scrapers/dynamic_app_adapter.py — Universal Dynamic App Adapter.

Autonomously analyzes unsupported / new apps and synthesizes dynamic
scrapers on the fly. Implements the spec from STEP 4.2.

Pipeline:
  1. Probe the target URL with httpx, capturing:
       - HTTP status, content-type, server header
       - HTML structure (presence of <article>, JSON-LD, OpenGraph)
       - Whether the site is JS-rendered (heuristic: presence of anti-bot
         markers OR extremely sparse HTML for a "real-looking" page)
  2. Detect the app's "type":
       - rss_atom    → wrap the existing news adapter
       - json_api    → JSON-path extraction
       - html_static → CSS-selector extraction (auto-derived from common
                       patterns: h1.title, .content, article, etc.)
       - html_js     → Playwright + heuristic content extraction
  3. Synthesize a dynamic scraper function (closure) that can be cached
     for the rest of the process lifetime and re-applied to similar URLs.
  4. Cache the synthesized scraper in an in-memory registry keyed by
     site fingerprint so subsequent calls don't re-probe.

This is the "brain" that lets Tony-EDWARD absorb a new B2B app (a
community forum, a vendor blog, an industry aggregator) without code
changes. Operators (admin role) may pin specific selectors via the
admin API; those overrides always win over auto-synthesized ones.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from app.scrapers.base import ScrapeResult, fetch, truncate_text

logger = logging.getLogger("tony_edward.adapter")
logger.setLevel(logging.INFO)


@dataclass
class AppProfile:
    """Detected profile for an unknown app/URL."""
    fingerprint: str
    host: str
    app_type: str               # rss_atom | json_api | html_static | html_js | unknown
    detected_selectors: dict[str, str] = field(default_factory=dict)
    uses_anti_bot: bool = False
    requires_js: bool = False
    sample_status: int = 0
    notes: list[str] = field(default_factory=list)


# Registry of synthesized scrapers: fingerprint -> async callable(url) -> ScrapeResult
_scraper_registry: dict[str, Callable[[str], Awaitable[ScrapeResult]]] = {}

# Admin-pinned overrides: host -> dict of selectors
_admin_overrides: dict[str, dict[str, str]] = {}


def site_fingerprint(url: str) -> str:
    """Stable identifier for a site — just the registered host.

    We do NOT include the URL path so all paths on a site share the
    same fingerprint and benefit from one synthesized scraper.
    """
    p = urlparse(url)
    return p.netloc.lower()


async def probe_app(url: str) -> AppProfile:
    """Send a HEAD+GET probe, classify the site, derive a profile."""
    fp = site_fingerprint(url)
    p = urlparse(url)
    profile = AppProfile(fingerprint=fp, host=p.netloc, app_type="unknown")

    # Try a direct fetch first
    res = await fetch(url, channel="adapter")
    profile.sample_status = res.status
    if not res.ok:
        profile.notes.append(f"probe_failed_status_{res.status}")
        # Try JS rendering as a last resort
        if res.status in (403, 429, 503) or not res.body_text:
            profile.requires_js = True
            profile.app_type = "html_js"
        return profile

    body = res.body_text
    ct = res.content_type

    # RSS / Atom detection
    if "xml" in ct or "<rss" in body[:500].lower() or "<feed" in body[:500].lower():
        profile.app_type = "rss_atom"
        profile.notes.append("rss_or_atom_feed_detected")
        return profile

    # JSON API detection
    if "json" in ct:
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                profile.app_type = "json_api"
                profile.notes.append("json_api_detected")
                # Identify likely fields
                if "results" in data:
                    profile.detected_selectors["items_path"] = "results"
                elif "data" in data:
                    profile.detected_selectors["items_path"] = "data"
                elif "items" in data:
                    profile.detected_selectors["items_path"] = "items"
        except json.JSONDecodeError:
            pass
        return profile

    # HTML detection — check if it looks like anti-bot
    sample = body[:8192].lower()
    antibot_markers = ("just a moment", "checking your browser", "cdn-cgi/challenge")
    if any(m in sample for m in antibot_markers):
        profile.uses_anti_bot = True
        profile.requires_js = True
        profile.app_type = "html_js"
        return profile

    # HTML — try to detect content containers
    candidates = {
        "title": [
            r'<h1[^>]*class="[^"]*title[^"]*"[^>]*>(.*?)</h1>',
            r'<h1[^>]*>(.*?)</h1>',
            r'<meta property="og:title" content="([^"]+)"',
        ],
        "content": [
            r'<article[^>]*>(.*?)</article>',
            r'<main[^>]*>(.*?)</main>',
            r'<div[^>]*class="[^"]*content[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*id="content"[^>]*>(.*?)</div>',
        ],
        "author": [
            r'<meta name="author" content="([^"]+)"',
            r'<span[^>]*class="[^"]*author[^"]*"[^>]*>(.*?)</span>',
        ],
        "date": [
            r'<time[^>]*datetime="([^"]+)"',
            r'<meta property="article:published_time" content="([^"]+)"',
        ],
    }
    detected = {}
    for field_name, patterns in candidates.items():
        for pat in patterns:
            m = re.search(pat, body, re.DOTALL | re.IGNORECASE)
            if m:
                detected[field_name] = pat
                break
    if detected:
        profile.app_type = "html_static"
        profile.detected_selectors = detected
        profile.notes.append(f"detected_{len(detected)}_selectors")
    else:
        # Sparse HTML on a real-looking URL → likely JS-rendered SPA
        if len(re.sub(r'<[^>]+>', '', body).strip()) < 500:
            profile.requires_js = True
            profile.app_type = "html_js"
            profile.notes.append("sparse_html_suggests_js_render")
        else:
            profile.app_type = "html_static"
            profile.detected_selectors = {
                "content": r'<body[^>]*>(.*?)</body>',
            }

    return profile


async def synthesize_scraper(profile: AppProfile) -> Callable[[str], Awaitable[ScrapeResult]]:
    """Given a profile, return a callable scraper function bound to it."""
    if profile.app_type == "rss_atom":
        from app.scrapers.channels import news as news_channel
        async def _rss(url: str) -> ScrapeResult:
            res = await fetch(url, channel="adapter")
            if not res.ok:
                return res
            items = await news_channel._parse_rss(res.body_text)
            out = [f"# Feed ({len(items)} items)"]
            for i, it in enumerate(items, 1):
                out.append(f"\n{i}. {it['title']}\n   {it['url']}\n   {it['summary'][:200]}")
            res.body_text = truncate_text("\n".join(out))
            res.meta["adapter"] = "rss_atom"
            return res
        return _rss

    if profile.app_type == "json_api":
        items_path = profile.detected_selectors.get("items_path", "results")
        async def _json(url: str) -> ScrapeResult:
            res = await fetch(url, channel="adapter")
            if not res.ok:
                return res
            try:
                data = json.loads(res.body_text)
                items = data.get(items_path, data) if isinstance(data, dict) else data
                res.body_text = truncate_text(json.dumps(items, indent=2)[:50_000])
                res.meta["adapter"] = "json_api"
            except json.JSONDecodeError:
                pass
            return res
        return _json

    if profile.app_type == "html_static":
        # Compile selector patterns once
        compiled = {k: re.compile(v, re.DOTALL | re.IGNORECASE)
                    for k, v in profile.detected_selectors.items()}
        async def _html(url: str) -> ScrapeResult:
            res = await fetch(url, channel="adapter")
            if not res.ok:
                return res
            body = res.body_text
            out_parts = []
            for field_name, pat in compiled.items():
                m = pat.search(body)
                if m:
                    val = re.sub(r'<[^>]+>', ' ', m.group(1))
                    val = re.sub(r'\s+', ' ', val).strip()
                    out_parts.append(f"## {field_name}\n{val}")
            res.body_text = truncate_text("\n\n".join(out_parts)) if out_parts else truncate_text(body)
            res.meta["adapter"] = "html_static"
            return res
        return _html

    if profile.app_type == "html_js":
        async def _js(url: str) -> ScrapeResult:
            res = await fetch(url, channel="adapter", require_js=True)
            res.meta["adapter"] = "html_js"
            if res.ok:
                # Strip tags from the rendered HTML
                text = re.sub(r'<script[^>]*>.*?</script>', '', res.body_text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r'<[^>]+>', ' ', text)
                res.body_text = truncate_text(re.sub(r'\s+', ' ', text).strip())
            return res
        return _js

    # Fallback — use generic web scraper
    from app.scrapers.channels.web import scrape_url as web_scrape
    return web_scrape


async def get_scraper_for(url: str) -> tuple[Callable[[str], Awaitable[ScrapeResult]], AppProfile]:
    """Return a cached scraper for the site, probing + synthesizing on miss."""
    fp = site_fingerprint(url)
    if fp in _scraper_registry:
        return _scraper_registry[fp], AppProfile(fingerprint=fp, host=fp, app_type="cached")

    # Check admin override — uses preconfigured selectors
    if fp in _admin_overrides:
        overrides = _admin_overrides[fp]
        profile = AppProfile(
            fingerprint=fp,
            host=fp,
            app_type=overrides.get("type", "html_static"),
            detected_selectors=overrides.get("selectors", {}),
            notes=["admin_override"],
        )
    else:
        profile = await probe_app(url)

    fn = await synthesize_scraper(profile)
    _scraper_registry[fp] = fn
    logger.info("adapter_synthesized host=%s type=%s notes=%s",
                fp, profile.app_type, profile.notes)
    return fn, profile


def set_admin_override(host: str, selectors: dict[str, str], app_type: str = "html_static") -> None:
    """Admin API call: pin a custom selector set for a host."""
    fp = host.lower()
    _admin_overrides[fp] = {"type": app_type, "selectors": selectors}
    # Invalidate cache so the override takes effect
    _scraper_registry.pop(fp, None)


def clear_admin_override(host: str) -> None:
    _admin_overrides.pop(host.lower(), None)
    _scraper_registry.pop(host.lower(), None)


def registry_stats() -> dict[str, Any]:
    return {
        "cached_sites": len(_scraper_registry),
        "admin_overrides": list(_admin_overrides.keys()),
    }
