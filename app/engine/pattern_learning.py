"""
app/engine/pattern_learning.py — Zero-Log Pattern Extractor.

Implements STEP 4.3:
  * Receive (query, signal_vector) tuples from search/predict handlers.
  * Compute cosine similarity to the K nearest stored patterns.
  * If the pattern shows significant novelty (similarity < threshold), persist
    only the abstract pattern vector + a non-reversible signature. NEVER
    persist the raw query, raw text, or any user-identifying data.
  * Format saved unique patterns into a standardized dataset structure
    ready for future LLM Fine-Tuning (JSONL with the OpenAI chat format).

Storage:
  * SQLite database at Settings.pattern_db_path.
  * Two tables:
      patterns(id, signature, vector_json, source, created_at, novelty_score)
      pattern_neighbors(pattern_id, neighbor_id, similarity)
  * A "fine_tune_export.jsonl" view is generated on demand via
    `export_fine_tune_dataset()`.

Auto-purge:
  * Patterns older than Settings.pattern_max_age_days are deleted by the
    auto-purge task (see app/storage/auto_purge.py).
  * When pattern count exceeds Settings.pattern_max_records, lowest-novelty
    patterns are pruned first.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from app.core.config import Settings, get_settings
from app.engine.model_router import embed_text, AuthContext

logger = logging.getLogger("tony_edward.pattern")
logger.setLevel(logging.INFO)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS patterns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signature       TEXT UNIQUE NOT NULL,
    vector          TEXT NOT NULL,                  -- JSON list[float]
    source          TEXT NOT NULL,
    task            TEXT NOT NULL,                  -- search | predict | classify
    created_at      REAL NOT NULL,
    novelty_score   REAL NOT NULL,
    nearest_id      INTEGER,
    nearest_sim     REAL,
    fine_tune_label TEXT
);
CREATE INDEX IF NOT EXISTS idx_patterns_created ON patterns(created_at);
CREATE INDEX IF NOT EXISTS idx_patterns_source ON patterns(source);
CREATE INDEX IF NOT EXISTS idx_patterns_novelty ON patterns(novelty_score);
"""


@dataclass
class PatternInput:
    """What a handler passes to pattern_learning.observe().

    CRITICAL: text must NOT be the raw user query. It should be the
    abstracted task description ("search for sentiment of company X")
    or the LLM-generated summary, NEVER the user's original phrasing.
    """
    task: str                          # search | predict | classify
    source: str                        # twitter | reddit | news | adapter
    text: str                          # abstracted description, NOT raw user input
    label: Optional[str] = None        # fine-tune label (e.g., expected answer class)


@dataclass
class PatternObservation:
    observed: bool
    novelty_score: float = 0.0
    nearest_similarity: float = 1.0
    reason: str = ""


@contextmanager
def _connect(db_path: str):
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(settings: Optional[Settings] = None) -> None:
    settings = settings or get_settings()
    os.makedirs(settings.storage_dir, exist_ok=True)
    with _connect(settings.pattern_db_path) as conn:
        conn.executescript(_SCHEMA)


def _signature(text: str, task: str, source: str) -> str:
    """SHA-256 of normalized text + task + source. Non-reversible."""
    normalized = " ".join(text.lower().split())
    material = f"{task}|{source}|{normalized}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    np_a = np.asarray(a, dtype=np.float32)
    np_b = np.asarray(b, dtype=np.float32)
    na = float(np.linalg.norm(np_a))
    nb = float(np.linalg.norm(np_b))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.dot(np_a, np_b) / (na * nb))


def _k_nearest(
    conn: sqlite3.Connection,
    vector: list[float],
    k: int = 5,
) -> list[tuple[int, float]]:
    """Return (id, similarity) for the k most similar stored patterns.

    Linear scan — fine for ≤200K patterns. For larger scale, an ANN
    index (FAISS / hnswlib) should be added as an upgrade path.
    """
    rows = conn.execute(
        "SELECT id, vector FROM patterns ORDER BY id DESC LIMIT 10000"
    ).fetchall()
    if not rows:
        return []
    scored = []
    for r in rows:
        try:
            v = json.loads(r["vector"])
            sim = _cosine(vector, v)
            scored.append((r["id"], sim))
        except (json.JSONDecodeError, TypeError):
            continue
    scored.sort(key=lambda x: -x[1])
    return scored[:k]


async def observe(
    ctx: AuthContext,
    pattern: PatternInput,
    settings: Optional[Settings] = None,
) -> PatternObservation:
    """Observe a pattern. Save only if novel. Returns observation result."""
    settings = settings or get_settings()
    init_db(settings)

    sig = _signature(pattern.text, pattern.task, pattern.source)

    # 1. Embed the abstracted text
    ok, vector, err = await embed_text(ctx, pattern.text[:8000],
                                       settings=settings)
    if not ok or not vector:
        # No embedding available — record signature only, no vector save
        logger.warning("pattern_embed_failed err=%s", err)
        return PatternObservation(observed=False, reason=f"embed_failed:{err}")

    with _connect(settings.pattern_db_path) as conn:
        # Already observed?
        row = conn.execute(
            "SELECT id FROM patterns WHERE signature = ?", (sig,)
        ).fetchone()
        if row:
            return PatternObservation(observed=False, reason="duplicate_signature")

        # Find nearest neighbors
        neighbors = _k_nearest(conn, vector, k=5)
        if neighbors:
            nearest_id, nearest_sim = neighbors[0]
        else:
            nearest_id, nearest_sim = None, 0.0

        # Novelty score = 1 - similarity_to_nearest
        novelty = max(0.0, 1.0 - nearest_sim)

        if novelty < settings.pattern_min_novelty_cosine:
            # Not novel enough — drop. Raw text NEVER persists.
            return PatternObservation(
                observed=False,
                novelty_score=novelty,
                nearest_similarity=nearest_sim,
                reason="below_novelty_threshold",
            )

        # Persist: signature + vector + abstracted label. NO raw query.
        conn.execute(
            """INSERT INTO patterns
               (signature, vector, source, task, created_at, novelty_score,
                nearest_id, nearest_sim, fine_tune_label)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sig,
                json.dumps(vector[:512] if len(vector) > 512 else vector),
                pattern.source,
                pattern.task,
                time.time(),
                novelty,
                nearest_id,
                nearest_sim,
                pattern.label,
            ),
        )

        return PatternObservation(
            observed=True,
            novelty_score=novelty,
            nearest_similarity=nearest_sim,
            reason="novel_pattern_persisted",
        )


def export_fine_tune_dataset(
    out_path: str,
    source: Optional[str] = None,
    limit: int = 10_000,
    settings: Optional[Settings] = None,
) -> dict:
    """Export unique patterns as a JSONL fine-tune dataset.

    Output format (OpenAI chat completions):
        {"messages": [{"role": "system", "content": "..."},
                       {"role": "user", "content": "<abstracted task>"},
                       {"role": "assistant", "content": "<label>"}]}

    NO raw user queries are included. Only the abstracted task description
    and the assigned label.
    """
    settings = settings or get_settings()
    init_db(settings)
    exported = 0
    skipped = 0
    with _connect(settings.pattern_db_path) as conn:
        query = "SELECT source, task, fine_tune_label, novelty_score FROM patterns"
        if source:
            query += " WHERE source = ?"
            rows = conn.execute(query + " ORDER BY novelty_score DESC LIMIT ?", (source, limit)).fetchall()
        else:
            rows = conn.execute(query + " ORDER BY novelty_score DESC LIMIT ?", (limit,)).fetchall()

        with open(out_path, "w", encoding="utf-8") as f:
            for r in rows:
                if not r["fine_tune_label"]:
                    skipped += 1
                    continue
                record = {
                    "messages": [
                        {"role": "system",
                         "content": f"You are a {r['task']} assistant for source {r['source']}."},
                        # NOTE: We do NOT export the raw text — only the signature-derived label.
                        {"role": "user",
                         "content": f"Process the latest {r['source']} signal."},
                        {"role": "assistant",
                         "content": r["fine_tune_label"]},
                    ],
                    "metadata": {
                        "source": r["source"],
                        "task": r["task"],
                        "novelty_score": r["novelty_score"],
                    },
                }
                f.write(json.dumps(record) + "\n")
                exported += 1

    return {
        "exported": exported,
        "skipped_no_label": skipped,
        "output_path": out_path,
    }


def stats(settings: Optional[Settings] = None) -> dict:
    settings = settings or get_settings()
    init_db(settings)
    with _connect(settings.pattern_db_path) as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM patterns").fetchone()["c"]
        by_source = {
            r["source"]: r["c"]
            for r in conn.execute(
                "SELECT source, COUNT(*) AS c FROM patterns GROUP BY source"
            ).fetchall()
        }
        avg_novelty = conn.execute(
            "SELECT AVG(novelty_score) AS a FROM patterns"
        ).fetchone()["a"] or 0.0
        db_size_bytes = os.path.getsize(settings.pattern_db_path)
        return {
            "total_patterns": total,
            "by_source": by_source,
            "avg_novelty": round(avg_novelty, 4),
            "db_size_bytes": db_size_bytes,
            "db_path": settings.pattern_db_path,
        }


def purge_stale(settings: Optional[Settings] = None) -> int:
    """Delete patterns older than Settings.pattern_max_age_days."""
    settings = settings or get_settings()
    cutoff = time.time() - settings.pattern_max_age_days * 86400
    with _connect(settings.pattern_db_path) as conn:
        cur = conn.execute(
            "DELETE FROM patterns WHERE created_at < ?", (cutoff,)
        )
        return cur.rowcount
