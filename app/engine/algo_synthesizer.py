"""
app/engine/algo_synthesizer.py — Universal Algorithm Synthesizer.

Implements STEP 4.2: autonomously analyze unsupported / new apps and
synthesize:
  1. A custom engagement metric (per-app heuristic based on detected
     page structure).
  2. A custom decay formula (exponential decay parameter tuned to the
     app's posting cadence).
  3. The dynamic scraper function (delegates to dynamic_app_adapter).

This module is the "brain" behind the engine's ability to absorb new
B2B platforms without code changes. Given an app's URL, it returns a
synthesized `EngagementAlgorithm` that downstream code uses to score
the app's content.
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.scrapers.base import ScrapeResult
from app.scrapers.dynamic_app_adapter import (
    AppProfile, probe_app, synthesize_scraper, get_scraper_for, site_fingerprint,
)

logger = logging.getLogger("tony_edward.synthesizer")
logger.setLevel(logging.INFO)


@dataclass
class EngagementAlgorithm:
    """Synthesized per-app algorithm."""
    host: str
    app_type: str
    engagement_formula: str        # human-readable expression
    decay_lambda: float             # per-day decay rate
    decay_formula: str
    detected_signals: list[str]    # which DOM signals feed engagement
    sample_scores: list[float] = field(default_factory=list)

    def decay(self, age_hours: float, base_score: float) -> float:
        """Apply time-decay to a base engagement score."""
        return base_score * math.exp(-self.decay_lambda * (age_hours / 24.0))


# Cache of synthesized algorithms per host
_algorithm_cache: dict[str, EngagementAlgorithm] = {}


def _detect_engagement_signals(profile: AppProfile, html: str = "") -> list[str]:
    """Identify which engagement signals are present in the app."""
    signals: list[str] = []
    body_lower = html.lower() if html else ""

    if any(s in body_lower for s in ('"likes"', "'likes'", "like-count", "data-likes")):
        signals.append("likes")
    if any(s in body_lower for s in ('"retweet"', "'retweet'", "retweet-count", "data-retweets")):
        signals.append("retweets")
    if any(s in body_lower for s in ('"comments"', "'comments'", "comment-count", "data-comments")):
        signals.append("comments")
    if any(s in body_lower for s in ('"shares"', "'shares'", "share-count", "data-shares")):
        signals.append("shares")
    if any(s in body_lower for s in ('"upvote"', "'upvote'", "data-score", "data-ups")):
        signals.append("upvotes")
    if any(s in body_lower for s in ('"views"', "'views'", "view-count", "data-views")):
        signals.append("views")
    if "datetime" in body_lower or "<time" in body_lower:
        signals.append("timestamp")

    # If no specific signals detected, infer from app_type
    if not signals:
        if profile.app_type == "rss_atom":
            signals = ["title", "summary", "published"]
        elif profile.app_type == "html_static":
            signals = ["title", "content_length"]
        elif profile.app_type == "json_api":
            signals = ["items", "fields"]
        else:
            signals = ["unknown"]

    return signals


def _synthesize_engagement_formula(signals: list[str]) -> str:
    """Build a human-readable formula string based on detected signals.

    The formula is heuristic but transparent — admins can audit and
    override it via the admin API.
    """
    parts = []
    weights = {
        "likes": 1.0,
        "retweets": 2.0,           # retweets are stronger virality signal
        "comments": 0.8,
        "shares": 1.5,
        "upvotes": 1.0,
        "views": 0.01,             # views scaled down (high magnitude)
    }
    used = []
    for s in signals:
        if s in weights:
            used.append(f"{weights[s]} * {s}")
    if not used:
        return "1.0  # no engagement signals detected, using unit weight"

    formula = " + ".join(used)
    return formula


def _estimate_decay_lambda(profile: AppProfile, signals: list[str]) -> float:
    """Estimate the per-day decay rate based on app type.

    Heuristics:
      * News sites decay fast (24-48h half-life) → high lambda
      * Forum posts decay slowly (1-2 week half-life) → low lambda
      * Video platforms decay medium (3-7 day half-life)
      * JSON APIs / unknown → moderate default
    """
    if profile.app_type == "rss_atom":
        return 0.7    # half-life ~1 day
    if profile.app_type == "json_api":
        return 0.3
    if profile.app_type == "html_static":
        # If we see forum-style engagement (upvotes, comments) → slow decay
        if "upvotes" in signals or "comments" in signals:
            return 0.15
        return 0.5
    if profile.app_type == "html_js":
        if "views" in signals:
            return 0.25   # video platform
        return 0.4
    return 0.3


async def synthesize_algorithm(url: str) -> tuple[EngagementAlgorithm, AppProfile]:
    """Top-level entry: probe + synthesize a per-app algorithm."""
    fp = site_fingerprint(url)

    # Check cache
    if fp in _algorithm_cache:
        cached = _algorithm_cache[fp]
        # Re-use the cached algorithm but re-probe to get fresh profile
        profile = await probe_app(url)
        return cached, profile

    # Probe the app to get its profile
    profile = await probe_app(url)

    # Fetch sample HTML for signal detection
    from app.scrapers.base import fetch
    sample_res = await fetch(url, channel="synthesizer")
    sample_html = sample_res.body_text if sample_res.ok else ""

    # Detect engagement signals
    signals = _detect_engagement_signals(profile, sample_html)

    # Synthesize formula
    formula = _synthesize_engagement_formula(signals)

    # Estimate decay rate
    decay_lambda = _estimate_decay_lambda(profile, signals)
    decay_formula = f"score * exp(-{decay_lambda:.3f} * age_days)"

    algo = EngagementAlgorithm(
        host=fp,
        app_type=profile.app_type,
        engagement_formula=formula,
        decay_lambda=decay_lambda,
        decay_formula=decay_formula,
        detected_signals=signals,
    )

    _algorithm_cache[fp] = algo
    logger.info(
        "algo_synthesized host=%s type=%s signals=%s decay=%.3f",
        fp, profile.app_type, signals, decay_lambda,
    )
    return algo, profile


def apply_engagement(algo: EngagementAlgorithm, signal_values: dict[str, float], age_hours: float = 0.0) -> float:
    """Compute the decayed engagement score for a piece of content.

    signal_values: dict mapping signal name (likes, comments, etc.) to count
    age_hours:     age of the content in hours
    """
    weights = {
        "likes": 1.0, "retweets": 2.0, "comments": 0.8, "shares": 1.5,
        "upvotes": 1.0, "views": 0.01, "title": 0.5, "content_length": 0.0001,
    }
    base = 0.0
    for sig, val in signal_values.items():
        w = weights.get(sig, 0.5)
        base += w * val
    return algo.decay(age_hours, base)


def list_synthesized() -> list[dict]:
    """Admin endpoint: list all synthesized algorithms."""
    return [
        {
            "host": a.host,
            "app_type": a.app_type,
            "engagement_formula": a.engagement_formula,
            "decay_lambda": a.decay_lambda,
            "decay_formula": a.decay_formula,
            "detected_signals": a.detected_signals,
        }
        for a in _algorithm_cache.values()
    ]


def clear_cache() -> int:
    n = len(_algorithm_cache)
    _algorithm_cache.clear()
    return n
