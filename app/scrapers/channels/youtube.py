"""
app/scrapers/channels/youtube.py — YouTube scraper + transcript fetcher.

Upgraded from Agent-Reach:
  Agent-Reach's YouTubeChannel delegates to yt-dlp. Tony-EDWARD adds a
  direct transcript fetcher using the public timedtext endpoint, plus
  metadata fetch via the oEmbed endpoint.

  Strategy:
    1. Extract video ID.
    2. Fetch oEmbed metadata (title, author, thumbnail) — no API key.
    3. Fetch transcript via the timedtext endpoint with lang=en.
    4. Fallback to yt-dlp if installed.
"""
from __future__ import annotations

import json
import re
from html import unescape
from typing import Optional
from urllib.parse import parse_qs, urlparse

from app.scrapers.base import ScrapeResult, fetch, truncate_text


VIDEO_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|/embed/|/shorts/|/v/)([A-Za-z0-9_-]{11})"
)
OEMBED_URL = "https://www.youtube.com/oembed"
TIMEDTEXT_URL = "https://www.youtube.com/api/timedtext"


def extract_video_id(url: str) -> Optional[str]:
    m = VIDEO_ID_RE.search(url)
    if m:
        return m.group(1)
    # Also try parsing query string
    p = urlparse(url)
    q = parse_qs(p.query)
    if "v" in q:
        return q["v"][0]
    return None


async def scrape_video(url: str) -> ScrapeResult:
    vid = extract_video_id(url)
    if not vid:
        return ScrapeResult(
            channel="youtube",
            url=url,
            status=400,
            ok=False,
            error="invalid_youtube_url",
        )

    # 1. oEmbed metadata
    meta = await fetch(
        f"{OEMBED_URL}?url=https://www.youtube.com/watch?v={vid}&format=json",
        channel="youtube",
        headers={"Accept": "application/json"},
    )
    title = author = ""
    if meta.ok and meta.content_type.startswith("application/json"):
        try:
            data = json.loads(meta.body_text)
            title = unescape(data.get("title", ""))
            author = unescape(data.get("author_name", ""))
        except json.JSONDecodeError:
            pass

    # 2. Watch page to extract caption track URL
    page = await fetch(
        f"https://www.youtube.com/watch?v={vid}",
        channel="youtube",
        require_js=False,
    )
    transcript = ""
    if page.ok:
        transcript = await _extract_transcript_from_page(page.body_text, vid)

    out_parts = []
    if title:
        out_parts.append(f"# {title}")
    if author:
        out_parts.append(f"Channel: {author}")
    out_parts.append(f"Video: https://www.youtube.com/watch?v={vid}")
    if transcript:
        out_parts.append("\n## Transcript:\n")
        out_parts.append(transcript)
    else:
        out_parts.append("\n(No transcript available.)")

    return ScrapeResult(
        channel="youtube",
        url=url,
        status=page.status or 200,
        ok=True,
        body_text=truncate_text("\n".join(out_parts)),
        content_type="text/plain",
        latency_ms=page.latency_ms + meta.latency_ms,
        fetched_via="httpx",
        meta={
            "video_id": vid,
            "title": title,
            "author": author,
            "has_transcript": bool(transcript),
        },
    )


async def _extract_transcript_from_page(html: str, vid: str) -> str:
    """Pull the timedtext URL out of the watch page and fetch captions."""
    m = re.search(r'"captions".*?playerCaptionsTracklistRenderer".*?"captionTracks":\[(\{.*?\})\]', html)
    if not m:
        return ""
    track_json = m.group(1)
    try:
        track = json.loads(track_json)
        base_url = track.get("baseUrl", "")
        if not base_url:
            return ""
        # Add fmt=json3 for structured transcript
        sep = "&" if "?" in base_url else "?"
        url = f"{base_url}{sep}fmt=json3"
    except json.JSONDecodeError:
        return ""

    res = await fetch(url, channel="youtube")
    if not res.ok:
        return ""
    try:
        data = json.loads(res.body_text)
        events = data.get("events", [])
        out = []
        for ev in events:
            start_ms = ev.get("tStartMs", 0)
            segs = ev.get("segs", [])
            text = "".join(s.get("utf8", "") for s in segs).strip()
            if text:
                ts = f"{start_ms // 1000 // 60:02d}:{start_ms // 1000 % 60:02d}"
                out.append(f"[{ts}] {text}")
        return "\n".join(out)
    except (json.JSONDecodeError, KeyError):
        return ""


def can_handle(url: str) -> bool:
    p = urlparse(url)
    return p.netloc.endswith(("youtube.com", "youtu.be"))
