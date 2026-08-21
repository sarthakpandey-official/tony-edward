"""
app/engine/sentiment_velocity.py — Sentiment + velocity analyzer.

Computes:
  * Sentiment polarity per text snippet (-1.0 to +1.0).
  * Velocity = rate of sentiment change across a time-ordered sequence.
  * Burst score = how concentrated mentions are in time.

Uses a lightweight lexicon-based approach (no LLM round-trip) so it
works at high throughput. LLM-based refinement is available via the
optional `refine_with_llm()` method when quality > throughput.

Zero-Logging: never persists input texts. The `analyze()` function
returns only aggregate scores + signal classifications.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional


# Compact lexicon — extendable. Subset of VADER + financial sentiment terms.
_POS_WORDS = {
    "growth", "surge", "soar", "soars", "soaring", "soared", "boost", "boosts",
    "win", "wins", "winning", "won", "gain", "gains", "gained", "gaining",
    "rise", "rises", "rising", "rose", "risen", "up", "bullish", "outperform",
    "beat", "beats", "beating", "beaten", "exceed", "exceeds", "exceeded",
    "strong", "stronger", "robust", "accelerate", "accelerates", "accelerated",
    "innovate", "innovation", "breakthrough", "success", "successful",
    "approve", "approval", "approved", "approving", "launch", "launches",
    "launched", "expanding", "expansion", "expand", "expands",
    "profit", "profitable", "upgrade", "upgraded", "buy", "bullish", "rally",
}
_NEG_WORDS = {
    "decline", "declines", "declined", "declining", "fall", "falls", "fell",
    "fallen", "falling", "drop", "drops", "dropped", "dropping",
    "loss", "losses", "lost", "losing", "bearish", "weak", "weaker", "weakness",
    "miss", "misses", "missing", "missed", "fail", "fails", "failed", "failing",
    "downgrade", "downgraded", "sell", "bearish", "plunge", "plunges", "plunged",
    "crash", "crashes", "crashed", "collapse", "collapses", "collapsed",
    "lawsuit", "sued", "sue", "sues", "investigation", "probe", "probed",
    "fraud", "scandal", "layoff", "layoffs", "fired", "fires", "firing",
    "delay", "delays", "delayed", "postpone", "postponed", "cancel", "canceled",
    "breach", "breached", "vulnerability", "vulnerable", "hack", "hacked",
    "down", "low", "lower", "lowest", "warning", "warned", "warns",
}
_NEGATE = {"not", "no", "never", "n't", "without", "hardly", "barely"}
_INTENSIFIERS = {"very", "highly", "extremely", "significantly", "remarkably"}

# Stop-words for tokenization
_STOP = set("the a an and or but in on at to of for is are was were be been being this that these those it its their his her our your".split())


@dataclass
class SentimentResult:
    polarity: float = 0.0          # -1.0 to +1.0
    magnitude: float = 0.0         # 0.0 to ∞, absolute intensity
    pos_hits: list[str] = field(default_factory=list)
    neg_hits: list[str] = field(default_factory=list)
    snippet_count: int = 0


@dataclass
class VelocityResult:
    velocities: list[float] = field(default_factory=list)   # per-step Δ sentiment
    burst_score: float = 0.0                                  # 0-1, concentration
    trend: str = "flat"                                      # up | down | flat
    net_change: float = 0.0
    volatility: float = 0.0


def _tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens = re.findall(r"[a-z]+(?:'[a-z]+)?", text)
    return [t for t in tokens if t not in _STOP]


def analyze(text: str) -> SentimentResult:
    """Lexicon-based sentiment. Returns polarity in [-1, +1]."""
    tokens = _tokenize(text)
    if not tokens:
        return SentimentResult()

    score = 0.0
    pos_hits: list[str] = []
    neg_hits: list[str] = []

    for i, tok in enumerate(tokens):
        weight = 1.0
        # Check previous 2 tokens for negation / intensifier
        if i >= 1:
            prev = tokens[i - 1]
            if prev in _NEGATE:
                weight *= -0.6
            elif prev in _INTENSIFIERS:
                weight *= 1.5
        if i >= 2 and tokens[i - 2] in _NEGATE:
            weight *= -0.4

        if tok in _POS_WORDS:
            score += weight
            if weight > 0:
                pos_hits.append(tok)
            else:
                neg_hits.append(tok)
        elif tok in _NEG_WORDS:
            score -= weight
            if weight > 0:
                neg_hits.append(tok)
            else:
                pos_hits.append(tok)

    # Normalize by sqrt(token_count) to dampen very long texts
    norm = max(1.0, math.sqrt(len(tokens)))
    polarity = max(-1.0, min(1.0, score / norm))
    magnitude = abs(score) / norm

    return SentimentResult(
        polarity=polarity,
        magnitude=magnitude,
        pos_hits=pos_hits[:20],
        neg_hits=neg_hits[:20],
        snippet_count=1,
    )


def analyze_batch(texts: list[str]) -> SentimentResult:
    """Aggregate sentiment across many snippets."""
    if not texts:
        return SentimentResult()
    polarities = []
    pos_hits_all: list[str] = []
    neg_hits_all: list[str] = []
    for t in texts:
        r = analyze(t)
        polarities.append(r.polarity)
        pos_hits_all.extend(r.pos_hits)
        neg_hits_all.extend(r.neg_hits)
    avg = sum(polarities) / len(polarities)
    mag = sum(abs(p) for p in polarities) / len(polarities)
    return SentimentResult(
        polarity=avg,
        magnitude=mag,
        pos_hits=list(set(pos_hits_all))[:20],
        neg_hits=list(set(neg_hits_all))[:20],
        snippet_count=len(texts),
    )


def velocity(time_series: list[tuple[float, float]]) -> VelocityResult:
    """Compute sentiment velocity across time-ordered (timestamp, polarity) pairs.

    timestamps are arbitrary monotonic numbers (epoch seconds or sequence idx).
    Returns per-step velocities, burst concentration, trend classification.
    """
    if len(time_series) < 2:
        return VelocityResult()

    # Sort by time
    series = sorted(time_series, key=lambda x: x[0])
    times = [s[0] for s in series]
    pols = [s[1] for s in series]

    # Δ per unit time
    velocities = []
    for i in range(1, len(series)):
        dt = max(1e-6, times[i] - times[i - 1])
        dp = pols[i] - pols[i - 1]
        velocities.append(dp / dt)

    # Burst score = fraction of total |Δp| in top 20% of steps
    abs_v = sorted([abs(v) for v in velocities], reverse=True)
    top_n = max(1, len(abs_v) // 5)
    top_sum = sum(abs_v[:top_n])
    total_sum = sum(abs_v) or 1e-9
    burst = min(1.0, top_sum / total_sum)

    # Trend classification
    net = pols[-1] - pols[0]
    if net > 0.15:
        trend = "up"
    elif net < -0.15:
        trend = "down"
    else:
        trend = "flat"

    # Volatility = stddev of velocities
    mean_v = sum(velocities) / len(velocities)
    var = sum((v - mean_v) ** 2 for v in velocities) / len(velocities)
    vol = math.sqrt(var)

    return VelocityResult(
        velocities=velocities,
        burst_score=burst,
        trend=trend,
        net_change=net,
        volatility=vol,
    )


async def refine_with_llm(text: str, ctx) -> dict:
    """Optional LLM-based refinement. Returns {polarity, summary}."""
    from app.engine.model_router import call_llm, LLMRequest
    req = LLMRequest(
        messages=[
            {"role": "system", "content": "You are a sentiment classifier. Return JSON {\"polarity\": float, \"summary\": str}."},
            {"role": "user", "content": f"Classify the sentiment of this text:\n\n{text[:2000]}"},
        ],
        task="classify",
        max_tokens=200,
        temperature=0.0,
    )
    resp = await call_llm(ctx, req)
    if not resp.ok:
        return {"polarity": 0.0, "summary": "", "error": resp.error}
    import json
    try:
        return json.loads(resp.text.strip().strip("`").strip())
    except json.JSONDecodeError:
        return {"polarity": 0.0, "summary": resp.text[:200]}
