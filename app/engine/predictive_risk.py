"""
app/engine/predictive_risk.py — Predictive risk scorer for B2B signals.

Given a corpus of scraped signals (texts + metadata), computes a risk
score that predicts business-disruption probability over a horizon.

Inputs:
  * list of (timestamp, source, polarity, magnitude) signals
  * Optional company / sector context

Output:
  * RiskScore(0-100) with component breakdown:
      - sentiment_trend        (negative slope → higher risk)
      - volatility             (high variance → higher risk)
      - burst_concentration    (sudden burst → higher risk)
      - source_diversity       (low diversity → lower confidence)
      - temporal_recency       (older signals → lower weight)

This is a transparent, rule-based scorer (not a black-box ML model)
so admins can audit and tune each component.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from app.engine.sentiment_velocity import velocity, VelocityResult


@dataclass
class Signal:
    timestamp: float          # epoch seconds
    source: str               # twitter | reddit | news | web | adapter
    polarity: float           # -1.0 to +1.0
    magnitude: float = 1.0    # confidence / weight
    text_hash: str = ""       # NEVER raw text — hash only


@dataclass
class RiskScore:
    overall: float = 0.0              # 0-100 (100 = highest risk)
    components: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0           # 0-1
    horizon_hours: int = 24
    n_signals: int = 0
    notes: list[str] = field(default_factory=list)


def score(signals: list[Signal], horizon_hours: int = 24) -> RiskScore:
    """Compute the predictive risk score for a signal set."""
    if not signals:
        return RiskScore(overall=0.0, confidence=0.0, n_signals=0,
                         notes=["no_signals"])

    # Sort by time
    signals = sorted(signals, key=lambda s: s.timestamp)
    n = len(signals)

    # 1. Sentiment trend component
    polarities_over_time = [(s.timestamp, s.polarity) for s in signals]
    vel = velocity(polarities_over_time)
    # Negative slope → higher risk
    # Map net_change from [-1, +1] to trend_risk in [0, 100]
    # net_change < -0.2 → ~90 risk; net_change ~0 → ~50; net_change > 0.2 → ~10
    trend_risk = 50.0 - (vel.net_change * 200.0)
    trend_risk = max(0.0, min(100.0, trend_risk))

    # 2. Volatility component
    # Higher volatility → higher risk, capped
    vol_risk = min(100.0, vel.volatility * 200.0)

    # 3. Burst concentration
    burst_risk = vel.burst_score * 100.0

    # 4. Source diversity (more diverse sources = higher confidence = lower risk)
    sources = set(s.source for s in signals)
    diversity_factor = min(1.0, len(sources) / 4.0)
    source_risk = (1.0 - diversity_factor) * 50.0

    # 5. Recency — older signals are weighted lower
    latest = max(s.timestamp for s in signals)
    avg_age = latest - sum(s.timestamp for s in signals) / n
    # avg_age in seconds; convert to hours
    avg_age_h = avg_age / 3600.0
    # Older than 7d → low recency → higher risk (stale data)
    recency_risk = min(100.0, avg_age_h * 100.0 / (24.0 * 7.0))

    # 6. Average polarity — extremely negative corpus = higher risk
    avg_pol = sum(s.polarity * s.magnitude for s in signals) / max(
        1e-9, sum(s.magnitude for s in signals)
    )
    polarity_risk = max(0.0, min(100.0, 50.0 - avg_pol * 100.0))

    # Weighted overall
    overall = (
        0.25 * trend_risk +
        0.20 * vol_risk +
        0.15 * burst_risk +
        0.10 * source_risk +
        0.10 * recency_risk +
        0.20 * polarity_risk
    )
    overall = max(0.0, min(100.0, overall))

    # Confidence — higher n + more diversity = higher confidence
    confidence = min(1.0, (n / 20.0) * diversity_factor)

    return RiskScore(
        overall=overall,
        components={
            "sentiment_trend": round(trend_risk, 2),
            "volatility": round(vol_risk, 2),
            "burst_concentration": round(burst_risk, 2),
            "source_diversity": round(source_risk, 2),
            "temporal_recency": round(recency_risk, 2),
            "polarity_avg": round(polarity_risk, 2),
            "avg_polarity": round(avg_pol, 3),
        },
        confidence=round(confidence, 3),
        horizon_hours=horizon_hours,
        n_signals=n,
        notes=[
            f"trend={vel.trend}",
            f"sources={sorted(sources)}",
            f"avg_age_hours={round(avg_age_h, 1)}",
        ],
    )


def from_texts(texts: list[str], source: str = "scrape") -> list[Signal]:
    """Convert raw texts to Signal objects for the risk scorer."""
    from app.engine.sentiment_velocity import analyze
    import time
    signals: list[Signal] = []
    now = time.time()
    for i, t in enumerate(texts):
        s = analyze(t)
        # Spread timestamps slightly to simulate real-time arrival
        ts = now - (len(texts) - i) * 60.0  # 1-minute intervals back in time
        signals.append(Signal(
            timestamp=ts,
            source=source,
            polarity=s.polarity,
            magnitude=s.magnitude,
            text_hash="",  # Never store text. Caller may add hash if desired.
        ))
    return signals
