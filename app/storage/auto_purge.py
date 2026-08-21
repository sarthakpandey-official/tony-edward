"""
app/storage/auto_purge.py — Local storage prune task.

Runs every few minutes. Keeps the local SQLite + storage_dir under the
20GB disk limit. SEPARATE from the 30-day sandbox cron which does deep
back-testing + B2 pruning.

  * Sum bytes in storage_dir.
  * If > 0.9 * limit_gb: vacuum SQLite + prune lowest-novelty patterns.
  * Prune patterns older than Settings.pattern_max_age_days.
  * Prune old fine-tune exports (>7 days).
  * Flush local JSONL buffers to B2.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from app.core.config import Settings, get_settings
from app.storage import pattern_db_cache, b2_manager
from app.engine.pattern_learning import purge_stale

logger = logging.getLogger("tony_edward.purge")
logger.setLevel(logging.INFO)


class AutoPurge:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.last_run: Optional[float] = None
        self.last_action: str = "idle"

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info("auto_purge_started interval=%ds limit=%dGB",
                    self.settings.auto_purge_check_interval_sec,
                    self.settings.auto_purge_storage_limit_gb)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._run_once()
            except Exception as exc:
                logger.warning("auto_purge_error err=%s", type(exc).__name__)
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=self.settings.auto_purge_check_interval_sec)
            except asyncio.TimeoutError:
                pass

    async def _run_once(self) -> None:
        self.last_run = time.time()
        try:
            deleted = await asyncio.get_event_loop().run_in_executor(
                None, purge_stale, self.settings
            )
            if deleted > 0:
                self.last_action = f"purged_stale_patterns:{deleted}"
                logger.info("purge_stale_patterns deleted=%d", deleted)
        except Exception as exc:
            logger.warning("purge_stale_failed err=%s", type(exc).__name__)

        used_gb = pattern_db_cache.storage_dir_size_gb(self.settings)
        limit_gb = float(self.settings.auto_purge_storage_limit_gb)
        soft_limit_gb = 0.9 * limit_gb
        hard_limit_gb = 0.95 * limit_gb

        if used_gb >= soft_limit_gb:
            total = pattern_db_cache.total_count(self.settings)
            target = int(total * 0.9)
            deleted = pattern_db_cache.prune_low_novelty(target, self.settings)
            logger.info("purge_low_novelty deleted=%d/%d", deleted, total)
            if used_gb >= hard_limit_gb:
                await asyncio.get_event_loop().run_in_executor(
                    None, pattern_db_cache.vacuum, self.settings
                )
                self.last_action = f"vacuum+prune:used={used_gb:.2f}GB"
            self._prune_old_exports()
            self.last_action = f"prune:used={used_gb:.2f}GB"

        # Also check B2 capacity — if B2 is near limit, prune oldest objects
        if self.settings.b2_configured:
            b2_status = b2_manager.status(self.settings)
            if b2_status["configured"] and b2_status["estimated_size_gb"] >= 0.9 * self.settings.b2_storage_limit_gb:
                await self._prune_old_b2_objects(fraction=0.1)

    def _prune_old_exports(self) -> None:
        export_dir = os.path.join(self.settings.storage_dir, "exports")
        if not os.path.isdir(export_dir):
            return
        cutoff = time.time() - 7 * 86400
        for entry in os.listdir(export_dir):
            path = os.path.join(export_dir, entry)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass

    async def _prune_old_b2_objects(self, fraction: float = 0.1) -> None:
        """Delete the oldest B2 objects when bucket approaches the limit."""
        def _do():
            objs = b2_manager.list_objects(prefix="patterns", settings=self.settings)
            objs.sort(key=lambda o: o.last_modified)
            n_to_delete = max(1, int(len(objs) * fraction))
            keys = [o.key for o in objs[:n_to_delete]]
            return b2_manager.delete_objects(keys, self.settings)
        result = await asyncio.get_event_loop().run_in_executor(None, _do)
        if result.get("ok"):
            logger.info("b2_prune_old_objects deleted=%d", result.get("deleted", 0))
            self.last_action = f"b2_prune_old:deleted={result.get('deleted', 0)}"

    def status(self) -> dict:
        return {
            "running": self._task is not None and not self._task.done(),
            "last_run": self.last_run,
            "last_action": self.last_action,
            "storage_used_gb": round(pattern_db_cache.storage_dir_size_gb(self.settings), 3),
            "storage_limit_gb": self.settings.auto_purge_storage_limit_gb,
            "pattern_count": pattern_db_cache.total_count(self.settings),
            "check_interval_sec": self.settings.auto_purge_check_interval_sec,
        }


_auto_purge: Optional[AutoPurge] = None


def get_auto_purge(settings: Optional[Settings] = None) -> AutoPurge:
    global _auto_purge
    if _auto_purge is None:
        _auto_purge = AutoPurge(settings)
    return _auto_purge


def reset_auto_purge() -> None:
    global _auto_purge
    _auto_purge = None
