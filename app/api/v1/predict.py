"""
app/api/v1/predict.py — Predictive analytics endpoints.

Routes:
  POST /v1/predict/sentiment   — lexicon-based sentiment of a text
  POST /v1/predict/sentiment/llm — LLM-refined sentiment (BYO API key for end-users)
  POST /v1/predict/risk        — multi-signal predictive risk score
  POST /v1/predict/synthesize  — synthesize a per-app engagement algorithm
  POST /v1/predict/velocity    — sentiment velocity across a time series
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.security import AuthContext, get_auth_context
from app.engine.sentiment_velocity import (
    analyze, analyze_batch, velocity, refine_with_llm,
)
from app.engine.predictive_risk import (
    score as risk_score,
    from_texts, Signal, RiskScore,
)
from app.engine.algo_synthesizer import synthesize_algorithm, apply_engagement, list_synthesized

router = APIRouter()
logger = logging.getLogger("tony_edward.api.predict")
logger.setLevel(logging.INFO)


# ---------------- models ----------------

class SentimentRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=50_000)


class BatchSentimentRequest(BaseModel):
    texts: list[str] = Field(..., min_length=1, max_length=500)


class RiskRequest(BaseModel):
    texts: list[str] = Field(..., min_length=1, max_length=500)
    source: str = Field("scrape", max_length=50)
    horizon_hours: int = Field(24, ge=1, le=24 * 30)


class SynthesizeRequest(BaseModel):
    url: str = Field(..., min_length=10, max_length=2000)


class ApplyEngagementRequest(BaseModel):
    url: str = Field(..., min_length=10, max_length=2000)
    signal_values: dict[str, float]
    age_hours: float = Field(0.0, ge=0.0)


class VelocityRequest(BaseModel):
    series: list[tuple[float, float]] = Field(..., min_length=2)


# ---------------- sentiment ----------------

@router.post("/sentiment")
async def predict_sentiment(req: SentimentRequest) -> dict:
    """Lexicon-based sentiment. No LLM round-trip — fast & free."""
    r = analyze(req.text)
    return {
        "polarity": round(r.polarity, 4),
        "magnitude": round(r.magnitude, 4),
        "pos_hits": r.pos_hits,
        "neg_hits": r.neg_hits,
    }


@router.post("/sentiment/llm")
async def predict_sentiment_llm(
    req: SentimentRequest,
    ctx: AuthContext = Depends(get_auth_context),
) -> dict:
    """LLM-refined sentiment. End-users MUST supply a BYO API key."""
    if not ctx.is_admin and not ctx.byo_api_key:
        raise HTTPException(status_code=400, detail="byo_api_key_required")
    result = await refine_with_llm(req.text, ctx)
    return result


@router.post("/sentiment/batch")
async def predict_sentiment_batch(req: BatchSentimentRequest) -> dict:
    """Aggregate sentiment across many snippets."""
    r = analyze_batch(req.texts)
    return {
        "polarity": round(r.polarity, 4),
        "magnitude": round(r.magnitude, 4),
        "pos_hits": r.pos_hits,
        "neg_hits": r.neg_hits,
        "snippet_count": r.snippet_count,
    }


# ---------------- velocity ----------------

@router.post("/velocity")
async def predict_velocity(req: VelocityRequest) -> dict:
    """Compute sentiment velocity across a time series."""
    v = velocity(req.series)
    return {
        "velocities": [round(x, 6) for x in v.velocities],
        "burst_score": round(v.burst_score, 4),
        "trend": v.trend,
        "net_change": round(v.net_change, 4),
        "volatility": round(v.volatility, 6),
    }


# ---------------- risk ----------------

@router.post("/risk")
async def predict_risk(req: RiskRequest) -> dict:
    """Compute a multi-component predictive risk score for a corpus."""
    signals = from_texts(req.texts, source=req.source)
    rs: RiskScore = risk_score(signals, horizon_hours=req.horizon_hours)
    return {
        "overall": round(rs.overall, 2),
        "components": rs.components,
        "confidence": rs.confidence,
        "horizon_hours": rs.horizon_hours,
        "n_signals": rs.n_signals,
        "notes": rs.notes,
    }


# ---------------- synthesizer ----------------

@router.post("/synthesize")
async def predict_synthesize(req: SynthesizeRequest) -> dict:
    """Synthesize a per-app engagement algorithm on the fly."""
    algo, profile = await synthesize_algorithm(req.url)
    return {
        "host": algo.host,
        "app_type": algo.app_type,
        "engagement_formula": algo.engagement_formula,
        "decay_lambda": algo.decay_lambda,
        "decay_formula": algo.decay_formula,
        "detected_signals": algo.detected_signals,
        "profile_notes": profile.notes,
        "profile_status": profile.sample_status,
    }


@router.post("/engagement")
async def predict_engagement(req: ApplyEngagementRequest) -> dict:
    """Apply a synthesized engagement algorithm to a content item."""
    algo, _ = await synthesize_algorithm(req.url)
    score_val = apply_engagement(algo, req.signal_values, req.age_hours)
    return {
        "host": algo.host,
        "score": round(score_val, 4),
        "decay_lambda": algo.decay_lambda,
        "age_hours": req.age_hours,
        "formula": algo.engagement_formula,
    }


@router.get("/synthesize/list")
async def list_synthesized_algos() -> dict:
    """List all synthesized algorithms. Public read."""
    return {"algorithms": list_synthesized()}
