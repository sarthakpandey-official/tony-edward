"""
app/storage/pattern_db_cache.py — Local SQLite pattern cache.

Hot read-path for patterns. Backblaze B2 is the durable off-host layer
(see b2_manager.py). Writes to local SQLite propagate to B2 via
b2_manager.upload_pattern_jsonl().

Zero-Logging: stores ONLY signatures + vectors + abstract labels.
NEVER stores raw user queries, raw scraped text, or auth tokens.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

from app.core.config import Settings, get_settings


_SCHEMA = """
CREATE TABLE IF NOT EXISTS patterns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signature       TEXT UNIQUE NOT NULL,
    vector          TEXT NOT NULL,
    source          TEXT NOT NULL,
    task            TEXT NOT NULL,
    created_at      REAL NOT NULL,
    novelty_score   REAL NOT NULL,
    nearest_id      INTEGER,
    nearest_sim     REAL,
    fine_tune_label TEXT,
    accuracy_score  REAL DEFAULT 0.5,
    last_evaluated_at REAL DEFAULT 0,
    b2_object_key   TEXT
);
CREATE INDEX IF NOT EXISTS idx_patterns_created ON patterns(created_at);
CREATE INDEX IF NOT EXISTS idx_patterns_source ON patterns(source);
CREATE INDEX IF NOT EXISTS idx_patterns_novelty ON patterns(novelty_score);
CREATE INDEX IF NOT EXISTS idx_patterns_accuracy ON patterns(accuracy_score);
CREATE INDEX IF NOT EXISTS idx_patterns_b2_key ON patterns(b2_object_key);
"""


@contextmanager
def _connect(db_path: str):
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def ensure_schema(settings: Optional[Settings] = None) -> None:
    settings = settings or get_settings()
    os.makedirs(settings.storage_dir, exist_ok=True)
    with _connect(settings.pattern_db_path) as conn:
        conn.executescript(_SCHEMA)


def get_pattern_by_id(pattern_id: int, settings: Optional[Settings] = None) -> Optional[dict]:
    settings = settings or get_settings()
    ensure_schema(settings)
    with _connect(settings.pattern_db_path) as conn:
        row = conn.execute("SELECT * FROM patterns WHERE id = ?", (pattern_id,)).fetchone()
        return dict(row) if row else None


def list_recent_patterns(
    source: Optional[str] = None,
    limit: int = 50,
    settings: Optional[Settings] = None,
) -> list[dict]:
    settings = settings or get_settings()
    ensure_schema(settings)
    with _connect(settings.pattern_db_path) as conn:
        if source:
            rows = conn.execute(
                "SELECT id, source, task, created_at, novelty_score, "
                "nearest_sim, accuracy_score, b2_object_key FROM patterns WHERE source = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (source, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, source, task, created_at, novelty_score, "
                "nearest_sim, accuracy_score, b2_object_key FROM patterns ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def total_count(settings: Optional[Settings] = None) -> int:
    settings = settings or get_settings()
    ensure_schema(settings)
    with _connect(settings.pattern_db_path) as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM patterns").fetchone()["c"]


def db_size_bytes(settings: Optional[Settings] = None) -> int:
    settings = settings or get_settings()
    if not os.path.exists(settings.pattern_db_path):
        return 0
    return os.path.getsize(settings.pattern_db_path)


def storage_dir_size_bytes(settings: Optional[Settings] = None) -> int:
    settings = settings or get_settings()
    total = 0
    for root, _, files in os.walk(settings.storage_dir):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def storage_dir_size_gb(settings: Optional[Settings] = None) -> float:
    return storage_dir_size_bytes(settings) / (1024 ** 3)


def prune_low_novelty(target_count: int, settings: Optional[Settings] = None) -> int:
    settings = settings or get_settings()
    ensure_schema(settings)
    with _connect(settings.pattern_db_path) as conn:
        current = conn.execute("SELECT COUNT(*) AS c FROM patterns").fetchone()["c"]
        if current <= target_count:
            return 0
        to_delete = current - target_count
        cur = conn.execute(
            "DELETE FROM patterns WHERE id IN ("
            "  SELECT id FROM patterns ORDER BY novelty_score ASC, created_at ASC LIMIT ?"
            ")",
            (to_delete,),
        )
        return cur.rowcount


def prune_low_accuracy(threshold: float = 0.3, settings: Optional[Settings] = None) -> int:
    settings = settings or get_settings()
    ensure_schema(settings)
    with _connect(settings.pattern_db_path) as conn:
        cur = conn.execute(
            "DELETE FROM patterns WHERE accuracy_score < ? AND last_evaluated_at > 0",
            (threshold,),
        )
        return cur.rowcount


def update_accuracy_scores(updates: dict, settings: Optional[Settings] = None) -> int:
    settings = settings or get_settings()
    ensure_schema(settings)
    now = time.time()
    n = 0
    with _connect(settings.pattern_db_path) as conn:
        for pid, score in updates.items():
            cur = conn.execute(
                "UPDATE patterns SET accuracy_score = ?, last_evaluated_at = ? WHERE id = ?",
                (float(score), now, int(pid)),
            )
            n += cur.rowcount
    return n


def update_b2_object_key(pattern_id: int, b2_key: str, settings: Optional[Settings] = None) -> bool:
    settings = settings or get_settings()
    ensure_schema(settings)
    with _connect(settings.pattern_db_path) as conn:
        cur = conn.execute(
            "UPDATE patterns SET b2_object_key = ? WHERE id = ?",
            (b2_key, pattern_id),
        )
        return cur.rowcount > 0


def vacuum(settings: Optional[Settings] = None) -> None:
    settings = settings or get_settings()
    with _connect(settings.pattern_db_path) as conn:
        conn.execute("VACUUM")


def list_patterns_for_backtest(limit: int = 1000, settings: Optional[Settings] = None) -> list[dict]:
    settings = settings or get_settings()
    ensure_schema(settings)
    with _connect(settings.pattern_db_path) as conn:
        rows = conn.execute(
            "SELECT id, source, task, fine_tune_label, accuracy_score, "
            "signature, created_at FROM patterns WHERE fine_tune_label IS NOT NULL "
            "ORDER BY last_evaluated_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_b2_object_keys(settings: Optional[Settings] = None) -> list[tuple[int, str]]:
    """Return (pattern_id, b2_object_key) pairs — used by sandbox prune."""
    settings = settings or get_settings()
    ensure_schema(settings)
    with _connect(settings.pattern_db_path) as conn:
        rows = conn.execute(
            "SELECT id, b2_object_key FROM patterns WHERE b2_object_key IS NOT NULL"
        ).fetchall()
        return [(r["id"], r["b2_object_key"]) for r in rows]
