"""Retention maintenance: delete expired originals and patch L1 metadata."""
from __future__ import annotations

import logging
import os
import re
import time as _time
from pathlib import Path as _Path
from typing import Any

logger = logging.getLogger(__name__)

_ORIG_FILE_RE = re.compile(r"^cap_[A-Z0-9]+_orig\.jpg$")


def select_expired(metadata: dict[str, Any], *, now: float) -> list[dict[str, Any]]:
    """Return the metadata.media.captures entries whose originals are due for deletion."""
    media = (metadata or {}).get("media") or {}
    captures = media.get("captures") or []
    out = []
    for c in captures:
        original = c.get("original_path")
        expires = c.get("original_expires_at")
        if original and isinstance(expires, (int, float)) and expires <= now:
            out.append(c)
    return out


def purge_expired(metadata: dict[str, Any], *, now: float) -> int:
    """Delete expired original files and patch the metadata in place.

    Returns the total bytes deleted.
    """
    expired = select_expired(metadata, now=now)
    total_bytes = 0
    for cap in expired:
        path = cap.get("original_path")
        if not path:
            continue
        try:
            sz = os.path.getsize(path)
        except OSError:
            sz = 0
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("retention.delete_failed path=%s err=%s", path, exc)
            continue
        total_bytes += sz
        cap["original_path"] = None
    return total_bytes


def purge_orphan_originals(
    resources_root: str | _Path,
    *,
    retention_days: int,
    now: float | None = None,
) -> dict:
    """Walk the resources tree and delete *_orig.jpg files older than retention_days.

    Returns a stats dict: {scanned: int, deleted: int, deleted_bytes: int, errors: int}.

    Thumbnails are never touched. The match is filename-based so we don't risk
    deleting unrelated files. If `resources_root` doesn't exist, returns zero stats.
    """
    root = _Path(resources_root)
    now_ts = _time.time() if now is None else float(now)
    cutoff = now_ts - max(0, int(retention_days)) * 86400.0

    stats = {"scanned": 0, "deleted": 0, "deleted_bytes": 0, "errors": 0}
    if not root.exists():
        return stats

    for path in root.rglob("cap_*_orig.jpg"):
        if not _ORIG_FILE_RE.match(path.name):
            continue
        stats["scanned"] += 1
        try:
            mtime = path.stat().st_mtime
        except OSError:
            stats["errors"] += 1
            continue
        if mtime > cutoff:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("retention.unlink_failed path=%s err=%s", path, exc)
            stats["errors"] += 1
            continue
        stats["deleted"] += 1
        stats["deleted_bytes"] += size

    return stats


__all__ = ["select_expired", "purge_expired", "purge_orphan_originals"]
