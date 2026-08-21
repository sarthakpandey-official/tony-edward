"""
app/scrapers/channels/reddit.py — Reddit scraper.

Upgraded from Agent-Reach:
  Agent-Reach's RedditChannel only checks whether `rdt-cli` is installed
  and has saved cookies. We add a direct httpx fetcher using Reddit's
  public .json endpoints (no login required for public subreddits).

  Strategy:
    1. For /r/{sub}/comments/{id}/ URLs: fetch the .json variant.
    2. For /r/{sub}/ URLs: fetch top.json with a 25-post limit.
    3. For /user/{u}/ URLs: fetch user .json.
    4. Honor REDDIT_PROXY if configured (for geofenced regions).
"""
from __future__ import annotations

import json
import re
from typing import Optional
from urllib.parse import urlparse

from app.scrapers.base import ScrapeResult, fetch, truncate_text


def _to_json_url(url: str) -> str:
    """Convert any reddit URL to its .json equivalent."""
    p = urlparse(url)
    path = p.path.rstrip("/")
    if not path.endswith(".json"):
        path = path + ".json"
    return f"{p.scheme}://{p.netloc}{path}"


def _flatten_comment(node: dict, depth: int = 0, out: list[str] = None) -> list[str]:
    if out is None:
        out = []
    if not isinstance(node, dict):
        return out
    data = node.get("data", {})
    body = data.get("body", "")
    author = data.get("author", "")
    score = data.get("score", 0)
    if body and body not in ("[deleted]", "[removed]"):
        indent = "  " * depth
        out.append(f"{indent}u/{author} (score={score}):\n{indent}{body}")
    replies = data.get("replies")
    if isinstance(replies, dict):
        children = replies.get("data", {}).get("children", [])
        for c in children:
            _flatten_comment(c, depth + 1, out)
    return out


async def scrape_post(url: str) -> ScrapeResult:
    json_url = _to_json_url(url)
    result = await fetch(
        json_url,
        channel="reddit",
        headers={"Accept": "application/json"},
    )
    if not result.ok:
        return result

    try:
        data = json.loads(result.body_text)
    except json.JSONDecodeError:
        return result

    if not isinstance(data, list) or len(data) < 2:
        result.body_text = truncate_text(result.body_text)
        return result

    # data[0] is the post, data[1] is the comment tree
    post_node = data[0].get("data", {}).get("children", [{}])[0]
    post = post_node.get("data", {})
    title = post.get("title", "")
    selftext = post.get("selftext", "")
    author = post.get("author", "")
    subreddit = post.get("subreddit_name_prefixed", "")
    ups = post.get("ups", 0)
    comments_node = data[1].get("data", {}).get("children", [])
    comments = []
    for c in comments_node:
        _flatten_comment(c, 0, comments)

    out_parts = [
        f"# {title}",
        f"subreddit: {subreddit} | author: u/{author} | ups: {ups}",
        "",
        selftext,
        "",
        "## Top comments:",
        "\n\n".join(comments[:20]),
    ]
    result.body_text = truncate_text("\n".join(out_parts))
    result.meta.update({
        "title": title,
        "author": author,
        "subreddit": subreddit,
        "ups": ups,
        "comment_count": len(comments),
    })
    return result


async def scrape_subreddit(sub: str, limit: int = 25) -> ScrapeResult:
    url = f"https://www.reddit.com/r/{sub}/top.json?limit={min(limit, 100)}&t=day"
    result = await fetch(
        url,
        channel="reddit",
        headers={"Accept": "application/json"},
    )
    if not result.ok:
        return result
    try:
        data = json.loads(result.body_text)
    except json.JSONDecodeError:
        return result

    children = data.get("data", {}).get("children", [])
    out = []
    for c in children:
        post = c.get("data", {})
        out.append(
            f"- [{post.get('title', '')}]({post.get('url', '')}) "
            f"({post.get('ups', 0)} ups, {post.get('num_comments', 0)} comments)"
        )
    result.body_text = truncate_text("\n".join(out))
    result.meta.update({"post_count": len(out), "subreddit": sub})
    return result


async def scrape_user(username: str) -> ScrapeResult:
    url = f"https://www.reddit.com/user/{username}/.json?limit=25"
    result = await fetch(url, channel="reddit")
    try:
        data = json.loads(result.body_text)
        children = data.get("data", {}).get("children", [])
        out = []
        for c in children:
            post = c.get("data", {})
            kind = c.get("kind", "")
            out.append(
                f"- [{kind}] {post.get('title') or post.get('body', '')[:200]} "
                f"in r/{post.get('subreddit', '')} ({post.get('ups', 0)} ups)"
            )
        result.body_text = truncate_text("\n".join(out))
        result.meta.update({"username": username, "activity_count": len(out)})
    except (json.JSONDecodeError, AttributeError):
        pass
    return result


def can_handle(url: str) -> bool:
    p = urlparse(url)
    return p.netloc.endswith("reddit.com") or p.netloc.endswith("redd.it")
