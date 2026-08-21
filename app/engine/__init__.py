"""Engine — predictive analytics + model routing + sandbox cron."""

from app.engine.model_router import call_llm, embed_text, LLMRequest, LLMResponse
from app.engine.sentiment_velocity import (
    analyze, analyze_batch, velocity, SentimentResult, VelocityResult,
)
from app.engine.predictive_risk import (
    score as risk_score, from_texts, Signal, RiskScore,
)
from app.engine.algo_synthesizer import (
    synthesize_algorithm, apply_engagement, EngagementAlgorithm,
)
from app.engine.pattern_learning import (
    observe, export_fine_tune_dataset, PatternInput, PatternObservation,
)
from app.engine.sandbox_cron import (
    SandboxCron, get_sandbox_cron, reset_sandbox_cron,
    run_sandbox_pass, list_run_reports,
)

__all__ = [
    "call_llm", "embed_text", "LLMRequest", "LLMResponse",
    "analyze", "analyze_batch", "velocity", "SentimentResult", "VelocityResult",
    "risk_score", "from_texts", "Signal", "RiskScore",
    "synthesize_algorithm", "apply_engagement", "EngagementAlgorithm",
    "observe", "export_fine_tune_dataset", "PatternInput", "PatternObservation",
    "SandboxCron", "get_sandbox_cron", "reset_sandbox_cron",
    "run_sandbox_pass", "list_run_reports",
]
