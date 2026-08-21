"""
app/storage/b2_manager.py — Backblaze B2 S3-compatible storage manager.

Single durable off-host storage layer for Tony-EDWARD patterns. Uses
boto3 with the S3-compatible API against Backblaze B2.

Operations:
  * upload_pattern_jsonl(record)  — append a pattern to a daily-rolled
                                    JSONL object on B2. Returns the B2 key.
  * upload_bytes(data, key)        — raw bytes upload (used for backups).
  * list_objects(prefix)           — list JSONL objects.
  * delete_object(key)             — delete one object.
  * delete_objects(keys)           — batch delete.
  * rewrite_object_filtering(key, predicate)
                                    — re-read, filter lines, re-upload
                                      (used by sandbox_cron to surgically
                                      remove specific signatures).
  * total_size_bytes()             — sum of all object sizes in bucket.
  * object_count()                 — number of objects in bucket.
  * status()                       — connection + size summary.

JSONL format on B2 (one line per pattern):
    {"prompt": "Process the latest {source} signal...", "completion": "<label>",
     "metadata": {"signature": "<sha256>", "novelty_score": 0.7, "source": "reddit"}}

Zero-Logging: prompt is abstracted task description, NEVER raw user query.
Signature is SHA-256, not the raw text.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from app.core.config import Settings, get_settings

logger = logging.getLogger("tony_edward.b2")
logger.setLevel(logging.INFO)

try:
    import boto3
    from botocore.client import Config as BotoConfig
    from botocore.exceptions import ClientError
    _HAS_BOTO3 = True
except ImportError:
    _HAS_BOTO3 = False
    logger.warning("boto3_not_installed — B2 disabled")


@dataclass
class B2ObjectInfo:
    key: str
    size: int
    last_modified: str


@dataclass
class B2UploadResult:
    ok: bool
    key: str = ""
    size_bytes: int = 0
    error: Optional[str] = None
    disabled: bool = False


# Per-process client cache (boto3 clients are thread-safe after creation)
_client_cache: dict[str, object] = {}
_client_lock = threading.Lock()


def _get_client(settings: Settings):
    """Lazily construct + cache the boto3 S3-compatible client."""
    if not _HAS_BOTO3:
        return None
    if not settings.b2_configured:
        return None
    cache_key = f"{settings.b2_endpoint_url}|{settings.b2_application_key_id}|{settings.b2_bucket_name}"
    with _client_lock:
        if cache_key in _client_cache:
            return _client_cache[cache_key]
        client = boto3.client(
            "s3",
            endpoint_url=settings.b2_endpoint_url,
            aws_access_key_id=settings.b2_application_key_id,
            aws_secret_access_key=settings.b2_application_key,
            region_name=settings.b2_region,
            config=BotoConfig(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                max_pool_connections=16,
            ),
        )
        _client_cache[cache_key] = client
        return client


def _daily_object_key(prefix: str = "patterns") -> str:
    """Daily-rolled JSONL object key. New file per UTC day per prefix."""
    date = time.strftime("%Y%m%d", time.gmtime())
    return f"{prefix}/{date}.jsonl"


def _deterministic_backup_key(data: bytes, prefix: str) -> str:
    digest = hashlib.sha256(data).hexdigest()[:32]
    date = time.strftime("%Y/%m/%d", time.gmtime())
    return f"{prefix}/{date}/{digest}.bin"


# ----------------- upload -----------------

def append_pattern_jsonl(
    record: dict,
    settings: Optional[Settings] = None,
) -> B2UploadResult:
    """Append a single pattern as a JSONL line to the daily B2 object.

    B2 (like S3) does not support true appends, so we:
      1. Read the current daily object (if exists).
      2. Append the new line.
      3. Re-upload as a single PUT.

    For high-throughput production, switch to per-pattern objects with
    content-addressed keys (see upload_pattern_dedicated).
    """
    settings = settings or get_settings()
    client = _get_client(settings)
    if client is None:
        return B2UploadResult(ok=False, disabled=True, error="b2_not_configured")

    line = json.dumps(record, sort_keys=True) + "\n"
    key = _daily_object_key()
    try:
        # Read existing
        try:
            resp = client.get_object(Bucket=settings.b2_bucket_name, Key=key)
            existing = resp["Body"].read()
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                existing = b""
            else:
                raise

        new_body = existing + line.encode("utf-8")
        client.put_object(
            Bucket=settings.b2_bucket_name,
            Key=key,
            Body=new_body,
            ContentType="application/x-ndjson",
        )
        return B2UploadResult(ok=True, key=key, size_bytes=len(new_body))
    except Exception as exc:
        logger.warning("b2_append_failed err=%s: %s", type(exc).__name__, exc)
        return B2UploadResult(ok=False, error=f"{type(exc).__name__}: {exc}")


def upload_pattern_dedicated(
    record: dict,
    settings: Optional[Settings] = None,
) -> B2UploadResult:
    """Upload a single pattern as its own content-addressed B2 object.

    Use this when fine-grained per-pattern deletion is needed (sandbox prune).
    Returns the dedicated B2 key which can later be passed to delete_object().
    """
    settings = settings or get_settings()
    client = _get_client(settings)
    if client is None:
        return B2UploadResult(ok=False, disabled=True, error="b2_not_configured")

    body = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    # Content-addressed key: prefix + date + first-16-chars-of-signature
    sig = record.get("metadata", {}).get("signature", "")
    sig_part = sig[:16] if sig else hashlib.sha256(body).hexdigest()[:16]
    date = time.strftime("%Y%m%d", time.gmtime())
    key = f"patterns_dedicated/{date}/{sig_part}.jsonl"
    try:
        client.put_object(
            Bucket=settings.b2_bucket_name,
            Key=key,
            Body=body,
            ContentType="application/x-ndjson",
        )
        return B2UploadResult(ok=True, key=key, size_bytes=len(body))
    except Exception as exc:
        logger.warning("b2_upload_dedicated_failed err=%s", type(exc).__name__)
        return B2UploadResult(ok=False, error=f"{type(exc).__name__}: {exc}")


def upload_bytes(
    data: bytes,
    key_prefix: str = "snapshots",
    settings: Optional[Settings] = None,
) -> B2UploadResult:
    """Raw bytes upload with a deterministic content-addressed key."""
    settings = settings or get_settings()
    client = _get_client(settings)
    if client is None:
        return B2UploadResult(ok=False, disabled=True, error="b2_not_configured")
    key = _deterministic_backup_key(data, key_prefix)
    try:
        client.put_object(
            Bucket=settings.b2_bucket_name,
            Key=key,
            Body=data,
            ContentType="application/octet-stream",
        )
        return B2UploadResult(ok=True, key=key, size_bytes=len(data))
    except Exception as exc:
        logger.warning("b2_upload_failed err=%s", type(exc).__name__)
        return B2UploadResult(ok=False, error=f"{type(exc).__name__}: {exc}")


def upload_file(file_path: str, key_prefix: str = "snapshots",
                settings: Optional[Settings] = None) -> B2UploadResult:
    settings = settings or get_settings()
    try:
        with open(file_path, "rb") as f:
            data = f.read()
        return upload_bytes(data, key_prefix, settings)
    except OSError as exc:
        return B2UploadResult(ok=False, error=f"{type(exc).__name__}: {exc}")


# ----------------- list / read / delete -----------------

def list_objects(prefix: str = "", settings: Optional[Settings] = None) -> list[B2ObjectInfo]:
    settings = settings or get_settings()
    client = _get_client(settings)
    if client is None:
        return []
    try:
        resp = client.list_objects_v2(Bucket=settings.b2_bucket_name, Prefix=prefix)
        return [
            B2ObjectInfo(
                key=o["Key"],
                size=o["Size"],
                last_modified=o["LastModified"].isoformat(),
            )
            for o in resp.get("Contents", [])
        ]
    except Exception as exc:
        logger.warning("b2_list_failed err=%s", type(exc).__name__)
        return []


def get_object_body(key: str, settings: Optional[Settings] = None) -> Optional[bytes]:
    settings = settings or get_settings()
    client = _get_client(settings)
    if client is None:
        return None
    try:
        resp = client.get_object(Bucket=settings.b2_bucket_name, Key=key)
        return resp["Body"].read()
    except Exception as exc:
        logger.warning("b2_get_failed key=%s err=%s", key, type(exc).__name__)
        return None


def delete_object(key: str, settings: Optional[Settings] = None) -> bool:
    settings = settings or get_settings()
    client = _get_client(settings)
    if client is None:
        return False
    try:
        client.delete_object(Bucket=settings.b2_bucket_name, Key=key)
        return True
    except Exception as exc:
        logger.warning("b2_delete_failed key=%s err=%s", key, type(exc).__name__)
        return False


def delete_objects(keys: list[str], settings: Optional[Settings] = None) -> dict:
    """Batch delete up to 1000 keys at once (S3 API limit)."""
    settings = settings or get_settings()
    client = _get_client(settings)
    if client is None:
        return {"ok": False, "disabled": True, "deleted": 0}
    if not keys:
        return {"ok": True, "deleted": 0}
    deleted_count = 0
    errors = []
    # Chunk to 1000
    for i in range(0, len(keys), 1000):
        chunk = keys[i:i + 1000]
        try:
            resp = client.delete_objects(
                Bucket=settings.b2_bucket_name,
                Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
            )
            deleted_count += len(chunk) - len(resp.get("Errors", []))
            if resp.get("Errors"):
                errors.extend([e.get("Key", "?") for e in resp["Errors"][:5]])
        except Exception as exc:
            logger.warning("b2_batch_delete_failed err=%s", type(exc).__name__)
    return {"ok": len(errors) == 0, "deleted": deleted_count, "errors": errors}


def rewrite_object_filtering(
    key: str,
    keep_predicate: Callable[[dict], bool],
    settings: Optional[Settings] = None,
) -> dict:
    """Read a JSONL object, drop lines where keep_predicate(line) is False,
    re-upload. Used by sandbox_cron to surgically remove pruned signatures.
    """
    settings = settings or get_settings()
    client = _get_client(settings)
    if client is None:
        return {"ok": False, "disabled": True}
    body = get_object_body(key, settings)
    if body is None:
        return {"ok": False, "error": "object_not_found"}
    lines_kept = 0
    lines_dropped = 0
    new_lines = []
    for line in body.decode("utf-8", errors="replace").split("\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            new_lines.append(line)
            continue
        if keep_predicate(obj):
            new_lines.append(line)
            lines_kept += 1
        else:
            lines_dropped += 1
    if lines_dropped == 0:
        return {"ok": True, "rewritten": False, "lines_kept": lines_kept}
    new_body = ("\n".join(new_lines) + "\n").encode("utf-8")
    try:
        client.put_object(
            Bucket=settings.b2_bucket_name,
            Key=key,
            Body=new_body,
            ContentType="application/x-ndjson",
        )
        return {"ok": True, "rewritten": True, "lines_kept": lines_kept, "lines_dropped": lines_dropped}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ----------------- accounting -----------------

def total_size_bytes(settings: Optional[Settings] = None) -> int:
    settings = settings or get_settings()
    objs = list_objects(settings=settings)
    return sum(o.size for o in objs)


def object_count(settings: Optional[Settings] = None) -> int:
    settings = settings or get_settings()
    return len(list_objects(settings=settings))


def estimated_size_gb(settings: Optional[Settings] = None) -> float:
    return total_size_bytes(settings) / (1024 ** 3)


def status(settings: Optional[Settings] = None) -> dict:
    settings = settings or get_settings()
    configured = settings.b2_configured and _HAS_BOTO3
    return {
        "configured": configured,
        "bucket": settings.b2_bucket_name,
        "endpoint": settings.b2_endpoint_url,
        "region": settings.b2_region,
        "object_count": object_count(settings) if configured else 0,
        "total_size_bytes": total_size_bytes(settings) if configured else 0,
        "estimated_size_gb": round(estimated_size_gb(settings), 4) if configured else 0.0,
        "limit_gb": settings.b2_storage_limit_gb,
        "headroom_gb": round(
            settings.b2_storage_limit_gb - estimated_size_gb(settings), 4
        ) if configured else float(settings.b2_storage_limit_gb),
        "boto3_available": _HAS_BOTO3,
    }


def backup_pattern_db(settings: Optional[Settings] = None) -> B2UploadResult:
    import os
    settings = settings or get_settings()
    if not os.path.exists(settings.pattern_db_path):
        return B2UploadResult(ok=False, error="db_not_found")
    return upload_file(settings.pattern_db_path, key_prefix="pattern_db_backup", settings=settings)
