"""
app/scrapers/channels/news.py — News scraper.

Uses RSS/Atom feeds plus the public news aggregators. No API key required.
For unknown news sources, falls through to the generic Web scraper.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import xml.etree.ElementTree as ET

from app.scrapers.base import ScrapeResult, fetch, truncate_text


@dataclass
class NewsItem:
    title: str
    url: str
    summary: str = ""
    published: str = ""


async def _parse_rss(xml_text: str) -> list[dict]:
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items

    # RSS 2.0
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if title or link:
            items.append({
                "title": title,
                "url": link,
                "summary": re.sub(r"<[^>]+>", "", desc)[:500],
                "published": pub,
            })

    # Atom
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.iterfind("atom:entry", ns):
        title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        link_el = entry.find("atom:link", ns)
        link = link_el.get("href", "") if link_el is not None else ""
        summary = (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
        published = (entry.findtext("atom:published", default="", namespaces=ns) or "").strip()
        if title or link:
            items.append({
                "title": title,
                "url": link,
                "summary": re.sub(r"<[^>]+>", "", summary)[:500],
                "published": published,
            })
    return items


async def scrape_google_news(query: str, limit: int = 20) -> ScrapeResult:
    """Search Google News RSS for a query."""
    rss_url = (
        "https://news.google.com/rss/search?q="
        + query.replace(" ", "+")
        + "&hl=en-US&gl=US&ceid=US:en"
    )
    res = await fetch(rss_url, channel="news")
    if not res.ok:
        return res
    items = await _parse_rss(res.body_text)
    items = items[:limit]
    out_lines = [f"# Google News — '{query}' ({len(items)} items)"]
    for i, it in enumerate(items, 1):
        out_lines.append(f"\n{i}. {it['title']}")
        if it["published"]:
            out_lines.append(f"   published: {it['published']}")
        if it["summary"]:
            out_lines.append(f"   {it['summary'][:200]}")
        out_lines.append(f"   url: {it['url']}")
    res.body_text = truncate_text("\n".join(out_lines))
    res.meta.update({"query": query, "item_count": len(items), "items": items})
    return res


async def scrape_article(url: str) -> ScrapeResult:
    """Fetch a single news article URL and extract readable text."""
    res = await fetch(url, channel="news", require_js=False)
    if not res.ok:
        return res

    # Extract <article> or main content via simple regex (no deps on readability lib)
    html = res.body_text
    # Try common article containers
    for pattern in (
        r'<article[^>]*>(.*?)</article>',
        r'<main[^>]*>(.*?)</main>',
        r'<div[^>]*class="[^"]*article[^"]*"[^>]*>(.*?)</div>',
    ):
        m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if m:
            text = re.sub(r'<script[^>]*>.*?</script>', '', m.group(1), flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            if len(text) > 200:
                res.body_text = truncate_text(text)
                return res

    # Fallback: strip tags from entire body
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    res.body_text = truncate_text(text)
    return res


def can_handle(url: str) -> bool:
    p = urlparse(url)
    news_domains = (
        "news.google.com", "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk",
        "nytimes.com", "washingtonpost.com", "theguardian.com", "bloomberg.com",
        "ft.com", "wsj.com", "cnbc.com", "techcrunch.com", "theverge.com",
        "arstechnica.com", "wired.com", "economist.com",
    )
    return any(p.netloc.endswith(d) for d in news_domains)
