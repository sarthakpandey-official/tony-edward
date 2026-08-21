"""Storage — local SQLite cache + Backblaze B2 + auto-purge."""

from app.storage.pattern_db_cache import (
    ensure_schema, list_recent_patterns, total_count,
    db_size_bytes, storage_dir_size_bytes, storage_dir_size_gb,
    prune_low_novelty, prune_low_accuracy, update_accuracy_scores,
    update_b2_object_key, vacuum, list_patterns_for_backtest,
)
from app.storage.b2_manager import (
    append_pattern_jsonl, upload_pattern_dedicated,
    upload_bytes, upload_file, backup_pattern_db,
    list_objects as b2_list_objects,
    get_object_body as b2_get_object_body,
    delete_object as b2_delete_object,
    delete_objects as b2_delete_objects,
    rewrite_object_filtering as b2_rewrite_object_filtering,
    total_size_bytes as b2_total_size_bytes,
    object_count as b2_object_count,
    estimated_size_gb as b2_estimated_size_gb,
    status as b2_status,
)
from app.storage.auto_purge import AutoPurge, get_auto_purge, reset_auto_purge

__all__ = [
    # pattern_db_cache
    "ensure_schema", "list_recent_patterns", "total_count",
    "db_size_bytes", "storage_dir_size_bytes", "storage_dir_size_gb",
    "prune_low_novelty", "prune_low_accuracy", "update_accuracy_scores",
    "update_b2_object_key", "vacuum", "list_patterns_for_backtest",
    # b2
    "append_pattern_jsonl", "upload_pattern_dedicated",
    "upload_bytes", "upload_file", "backup_pattern_db",
    "b2_list_objects", "b2_get_object_body",
    "b2_delete_object", "b2_delete_objects", "b2_rewrite_object_filtering",
    "b2_total_size_bytes", "b2_object_count", "b2_estimated_size_gb", "b2_status",
    # auto_purge
    "AutoPurge", "get_auto_purge", "reset_auto_purge",
]
