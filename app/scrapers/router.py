"""
app/scrapers/router.py — Universal scraper router.

Given a URL, route to the correct upgraded scraper module. Falls back to:
  1. Specific channel (twitter/reddit/youtube/news) if can_handle() matches.
  2. dynamic_app_adapter for unknown hosts (auto-synthesizes a scraper).
  3. Generic web channel as last resort.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from app.scrapers.base import ScrapeResult
from app.scrapers.channels import twitter, reddit, youtube, news, web
from app.scrapers.dynamic_app_adapter import get_scraper_for


async def route_search(query: str, source: Optional[str] = None, limit: int = 20) -> ScrapeResult:
    """Search across a named source. Falls back to Google News."""
    source = (source or "news").lower()
    if source in ("news", "google_news"):
        return await news.scrape_google_news(query, limit=limit)
    if source == "reddit":
        # Treat query as subreddit
        return await reddit.scrape_subreddit(query, limit=limit)
    if source == "twitter":
        return await twitter.scrape_user(query)
    # Default — Google News
    return await news.scrape_google_news(query, limit=limit)


async def route_url(url: str) -> ScrapeResult:
    """Given a URL, pick the right scraper."""
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return ScrapeResult(channel="router", url=url, status=400, ok=False, error="invalid_url")

    host = p.netloc.lower()

    if host.endswith(("twitter.com", "x.com", "t.co")):
        return await twitter.scrape_tweet(url)
    if host.endswith(("reddit.com", "redd.it")):
        return await reddit.scrape_post(url)
    if host.endswith(("youtube.com", "youtu.be")):
        return await youtube.scrape_video(url)
    if news.can_handle(url):
        return await news.scrape_article(url)

    # Unknown host — try the dynamic adapter
    try:
        fn, profile = await get_scraper_for(url)
        return await fn(url)
    except Exception as exc:
        # Last resort: generic web
        return await web.scrape_url(url)


async def route_adaptive(url: str) -> ScrapeResult:
    """Force the dynamic adapter (skip known-channel fast path)."""
    try:
        fn, _ = await get_scraper_for(url)
        return await fn(url)
    except Exception:
        return await web.scrape_url(url)
