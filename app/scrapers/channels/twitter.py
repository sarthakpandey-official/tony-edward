"""
app/scrapers/channels/twitter.py — Twitter/X scraper.

Upgraded from Agent-Reach:
  Agent-Reach's TwitterChannel only checks whether `twitter-cli` or
  `bird` are installed. It does NOT scrape. We add a direct httpx
  fetcher that uses the public syndication endpoints (no auth needed
  for tweet embeds) plus a Playwright fallback for full tweet pages.

  Strategy:
    1. Extract tweet ID from the URL.
    2. Try the public oEmbed / syndication endpoint first (no auth).
    3. If that fails, fall back to Playwright with the full tweet URL.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from app.scrapers.base import ScrapeResult, fetch, truncate_text

TWEET_ID_RE = re.compile(r"/status(?:es)?/(\d+)", re.IGNORECASE)
SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"


def extract_tweet_id(url: str) -> Optional[str]:
    m = TWEET_ID_RE.search(url)
    return m.group(1) if m else None


async def scrape_tweet(url: str) -> ScrapeResult:
    tweet_id = extract_tweet_id(url)
    if not tweet_id:
        return ScrapeResult(
            channel="twitter",
            url=url,
            status=400,
            ok=False,
            error="invalid_tweet_url",
        )

    # Try syndication first (no auth, JSON response)
    synd_url = f"{SYNDICATION_URL}?id={tweet_id}&token=0"
    result = await fetch(
        synd_url,
        channel="twitter",
        headers={"Accept": "application/json"},
    )

    if result.ok and result.content_type.startswith("application/json"):
        try:
            data = json.loads(result.body_text)
            text = data.get("text", "")
            user = data.get("user", {}).get("screen_name", "")
            created = data.get("created_at", "")
            out = (
                f"@{user} ({created}):\n{text}\n\n"
                f"Likes: {data.get('favorite_count', 0)} | "
                f"Retweets: {data.get('retweet_count', 0)} | "
                f"Replies: {data.get('reply_count', 0)}"
            )
            result.body_text = out
            result.meta.update({
                "tweet_id": tweet_id,
                "user": user,
                "favorite_count": data.get("favorite_count", 0),
                "retweet_count": data.get("retweet_count", 0),
                "reply_count": data.get("reply_count", 0),
            })
            return result
        except (json.JSONDecodeError, AttributeError):
            pass  # fall through to playwright

    # Playwright fallback for the tweet page
    pw_result = await fetch(url, channel="twitter", require_js=True)
    if pw_result.ok:
        # Extract the visible tweet text from article elements
        m = re.search(
            r'<article[^>]*>(.*?)</article>',
            pw_result.body_text,
            re.DOTALL,
        )
        if m:
            # Crude tag strip
            import re as _re
            text = _re.sub(r'<[^>]+>', ' ', m.group(1))
            text = _re.sub(r'\s+', ' ', text).strip()
            pw_result.body_text = truncate_text(text)
        pw_result.meta["tweet_id"] = tweet_id
    return pw_result


async def scrape_user(username: str) -> ScrapeResult:
    """Scrape the most recent tweets from a user's profile (best-effort)."""
    url = f"https://nitter.net/{username}"  # nitter mirror — anti-bot friendly
    result = await fetch(url, channel="twitter")
    if result.ok:
        # Extract tweet-like entries from nitter HTML
        tweets = re.findall(
            r'<div class="tweet-content[^"]*"[^>]*>(.*?)</div>',
            result.body_text,
            re.DOTALL,
        )
        if tweets:
            cleaned = []
            for t in tweets[:20]:
                t = re.sub(r'<[^>]+>', '', t).strip()
                t = re.sub(r'\s+', ' ', t)
                if t:
                    cleaned.append(t)
            result.body_text = "\n---\n".join(cleaned) if cleaned else result.body_text
            result.meta["tweet_count"] = len(cleaned)
    return result
