"""
app/engine/sandbox_cron.py — 30-day Sandbox back-testing + pruning engine.

Runs every 30 days (configurable via SANDBOX_CRON_INTERVAL_DAYS). Each run:

  1. Pulls labeled patterns from local SQLite.
  2. For each pattern, runs a back-test:
       - Re-derive sentiment from the abstracted label (lexicon-based, no LLM).
       - Compare against an expected ground-truth derived from the label.
       - Update the pattern's `accuracy_score` in SQLite.
  3. Prune patterns whose accuracy_score is below threshold from:
       - Local SQLite (DELETE WHERE accuracy_score < threshold)
       - Backblaze B2 (delete dedicated per-pattern objects + rewrite
         daily JSONL files to remove pruned signatures)
  4. Persist a run report to storage_dir/sandbox_runs/<timestamp>.json
     (aggregate-only — no signatures, no labels).

Triggered automatically by background loop OR manually via admin dashboard.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from app.core.config import Settings, get_settings
from app.engine.sentiment_velocity import analyze as sentiment_analyze
from app.storage.pattern_db_cache import (
    list_patterns_for_backtest, update_accuracy_scores, prune_low_accuracy,
    total_count, list_b2_object_keys,
)
from app.storage import b2_manager

logger = logging.getLogger("tony_edward.sandbox")
logger.setLevel(logging.INFO)


@dataclass
class SandboxRunReport:
    started_at: float
    finished_at: float = 0.0
    duration_sec: float = 0.0
    patterns_evaluated: int = 0
    patterns_pruned_local: int = 0
    patterns_pruned_b2_objects: int = 0
    patterns_pruned_b2_lines: int = 0
    avg_accuracy_before: float = 0.0
    avg_accuracy_after: float = 0.0
    accuracy_distribution: dict = field(default_factory=dict)
    error: Optional[str] = None


def _classify_label(label: str) -> tuple[str, float]:
    """Heuristic ground-truth derivation from a fine_tune_label."""
    if not label:
        return "neutral", 0.0
    text = label.lower()
    pos_markers = ("positive", "bullish", "growth", "upgrade", "strong", "buy")
    neg_markers = ("negative", "bearish", "decline", "downgrade", "weak", "sell")
    if any(m in text for m in pos_markers):
        return "positive", 0.5
    if any(m in text for m in neg_markers):
        return "negative", -0.5
    return "neutral", 0.0


def _evaluate_pattern(pattern_row: dict) -> float:
    """Return an accuracy score [0.0, 1.0] for a single pattern."""
    label = pattern_row.get("fine_tune_label") or ""
    if not label:
        return 0.5
    pred = sentiment_analyze(label).polarity
    _, expected = _classify_label(label)
    diff = abs(pred - expected)
    return max(0.0, min(1.0, 1.0 - diff))


async def run_sandbox_pass(settings: Optional[Settings] = None) -> SandboxRunReport:
    """Run one full sandbox evaluation + pruning pass."""
    settings = settings or get_settings()
    report = SandboxRunReport(started_at=time.time())

    try:
        patterns = list_patterns_for_backtest(
            limit=settings.sandbox_min_patterns_to_eval * 4,
            settings=settings,
        )
        if len(patterns) < settings.sandbox_min_patterns_to_eval:
            logger.info("sandbox_skipped_too_few_patterns count=%d", len(patterns))
            report.finished_at = time.time()
            report.duration_sec = report.finished_at - report.started_at
            report.error = f"insufficient_patterns:{len(patterns)}"
            _persist_report(report, settings)
            return report

        updates: dict[int, float] = {}
        scores_before: list[float] = []
        for p in patterns:
            score = _evaluate_pattern(p)
            updates[p["id"]] = score
            scores_before.append(score)

        update_accuracy_scores(updates, settings)
        report.patterns_evaluated = len(updates)
        report.avg_accuracy_before = (
            sum(scores_before) / len(scores_before) if scores_before else 0.0
        )

        # 1. Prune low-accuracy patterns locally
        deleted_local = prune_low_accuracy(settings.sandbox_accuracy_threshold, settings)
        report.patterns_pruned_local = deleted_local

        # 2. Prune from B2 — collect signatures of low-accuracy patterns
        low_acc_signatures = set()
        low_acc_pattern_ids = set()
        for pid, score in updates.items():
            if score < settings.sandbox_accuracy_threshold:
                low_acc_pattern_ids.add(pid)
                # Find the signature for this pattern_id
                for p in patterns:
                    if p["id"] == pid:
                        sig = p.get("signature", "")
                        if sig:
                            low_acc_signatures.add(sig[:16])  # B2 keys use first 16 chars
                        break

        # 2a. Delete dedicated per-pattern B2 objects
        if low_acc_pattern_ids and settings.b2_configured:
            # Get B2 object keys for these patterns
            b2_keys = list_b2_object_keys(settings)
            keys_to_delete = []
            for pid, key in b2_keys:
                if pid in low_acc_pattern_ids:
                    keys_to_delete.append(key)
            if keys_to_delete:
                result = b2_manager.delete_objects(keys_to_delete, settings)
                report.patterns_pruned_b2_objects = result.get("deleted", 0)

            # 2b. Rewrite daily JSONL objects to remove pruned signatures
            daily_objects = b2_manager.list_objects(prefix="patterns/", settings=settings)
            rewritten_lines = 0
            for obj in daily_objects:
                if not obj.key.endswith(".jsonl"):
                    continue
                # Predicate: keep lines whose signature is NOT in low_acc_signatures
                def keep(obj_line, _sig_set=low_acc_signatures):
                    try:
                        sig = obj_line.get("metadata", {}).get("signature", "")[:16]
                        return sig not in _sig_set
                    except Exception:
                        return True
                result = b2_manager.rewrite_object_filtering(obj.key, keep, settings)
                if result.get("ok") and result.get("rewritten"):
                    rewritten_lines += result.get("lines_dropped", 0)
            report.patterns_pruned_b2_lines = rewritten_lines

        # 3. Recompute avg accuracy after prune
        remaining = [(pid, s) for pid, s in updates.items()
                     if s >= settings.sandbox_accuracy_threshold]
        report.avg_accuracy_after = (
            sum(s for _, s in remaining) / len(remaining) if remaining else 0.0
        )
        report.accuracy_distribution = _distribution(scores_before)

    except Exception as exc:
        logger.exception("sandbox_run_failed err=%s", type(exc).__name__)
        report.error = f"{type(exc).__name__}: {exc}"

    report.finished_at = time.time()
    report.duration_sec = report.finished_at - report.started_at
    _persist_report(report, settings)
    logger.info(
        "sandbox_run_complete evaluated=%d pruned_local=%d pruned_b2_obj=%d pruned_b2_lines=%d dur=%.1fs",
        report.patterns_evaluated,
        report.patterns_pruned_local,
        report.patterns_pruned_b2_objects,
        report.patterns_pruned_b2_lines,
        report.duration_sec,
    )
    return report


def _distribution(scores: list[float]) -> dict:
    buckets = {"high_>=0.8": 0, "mid_0.5_0.8": 0, "low_0.3_0.5": 0, "fail_<0.3": 0}
    for s in scores:
        if s >= 0.8:
            buckets["high_>=0.8"] += 1
        elif s >= 0.5:
            buckets["mid_0.5_0.8"] += 1
        elif s >= 0.3:
            buckets["low_0.3_0.5"] += 1
        else:
            buckets["fail_<0.3"] += 1
    return buckets


def _persist_report(report: SandboxRunReport, settings: Settings) -> None:
    run_dir = os.path.join(settings.storage_dir, "sandbox_runs")
    os.makedirs(run_dir, exist_ok=True)
    path = os.path.join(run_dir, f"run_{int(report.started_at)}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(report), f, indent=2, sort_keys=True)
    except OSError as exc:
        logger.warning("sandbox_report_persist_failed err=%s", type(exc).__name__)


def list_run_reports(settings: Optional[Settings] = None, limit: int = 20) -> list[dict]:
    settings = settings or get_settings()
    run_dir = os.path.join(settings.storage_dir, "sandbox_runs")
    if not os.path.isdir(run_dir):
        return []
    files = sorted(os.listdir(run_dir), reverse=True)[:limit]
    out = []
    for fn in files:
        path = os.path.join(run_dir, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                out.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    return out


class SandboxCron:
    """Periodic 30-day sandbox loop."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.last_run: Optional[float] = None
        self.last_report: Optional[SandboxRunReport] = None
        self.next_run: Optional[float] = None

    async def start(self) -> None:
        if not self.settings.sandbox_cron_enabled:
            logger.info("sandbox_cron_disabled_by_config")
            return
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        self.next_run = time.time() + self.settings.sandbox_cron_interval_days * 86400
        logger.info("sandbox_cron_started interval_days=%d",
                    self.settings.sandbox_cron_interval_days)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def trigger_now(self) -> SandboxRunReport:
        logger.info("sandbox_cron_manual_trigger")
        return await run_sandbox_pass(self.settings)

    async def _loop(self) -> None:
        interval_sec = self.settings.sandbox_cron_interval_days * 86400
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval_sec)
                if self._stop.is_set():
                    return
            except asyncio.TimeoutError:
                pass
            self.last_report = await run_sandbox_pass(self.settings)
            self.last_run = time.time()
            self.next_run = self.last_run + interval_sec

    def status(self) -> dict:
        return {
            "running": self._task is not None and not self._task.done(),
            "enabled": self.settings.sandbox_cron_enabled,
            "interval_days": self.settings.sandbox_cron_interval_days,
            "accuracy_threshold": self.settings.sandbox_accuracy_threshold,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "last_summary": (
                {
                    "patterns_evaluated": self.last_report.patterns_evaluated,
                    "patterns_pruned_local": self.last_report.patterns_pruned_local,
                    "patterns_pruned_b2_objects": self.last_report.patterns_pruned_b2_objects,
                    "patterns_pruned_b2_lines": self.last_report.patterns_pruned_b2_lines,
                    "avg_accuracy_before": round(self.last_report.avg_accuracy_before, 4),
                    "avg_accuracy_after": round(self.last_report.avg_accuracy_after, 4),
                    "duration_sec": round(self.last_report.duration_sec, 2),
                    "error": self.last_report.error,
                }
                if self.last_report else None
            ),
        }


_sandbox_cron: Optional[SandboxCron] = None


def get_sandbox_cron(settings: Optional[Settings] = None) -> SandboxCron:
    global _sandbox_cron
    if _sandbox_cron is None:
        _sandbox_cron = SandboxCron(settings)
    return _sandbox_cron


def reset_sandbox_cron() -> None:
    global _sandbox_cron
    _sandbox_cron = None
