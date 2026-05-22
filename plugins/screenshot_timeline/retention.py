"""Retention maintenance: delete expired originals and patch L1 metadata."""
from __future__ import annotations

import logging
import os
import re
import time as _time
from pathlib import Path as _Path
from typing import Any

logger = logging.getLogger(__name__)

# Legacy filename layout: <resources_root>/<YYYY>/<MM>/<DD>/cap_<ULID>_orig.jpg
# Originals and thumbnails lived in the same date directory, told apart by
# the _orig / _thumb suffix.
_LEGACY_ORIG_FILE_RE = re.compile(r"^cap_[A-Z0-9]+_orig\.jpg$")

# New layout: <resources_root>/originals/<YYYY>/<MM>/<DD>/<capture_id>.jpg
# Thumbnails live in a sibling `thumbnails/` tree with the same filename.
# Everything under `originals/` is fair game for retention.
_NEW_JPG_RE = re.compile(r"^.+\.jpg$", re.IGNORECASE)


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
    """Walk the resources tree and delete expired originals.

    Returns a stats dict: {scanned: int, deleted: int, deleted_bytes: int, errors: int}.

    Thumbnails are never touched.

    Two layouts are scanned for backward compatibility:
      1. New: ``<root>/originals/YYYY/MM/DD/<capture_id>.jpg``
         Everything under ``originals/`` is fair game.
      2. Legacy: ``<root>/YYYY/MM/DD/cap_<ULID>_orig.jpg``
         Pre-0.1.8 captures had originals and thumbnails interleaved in
         the same date folder, distinguished by an ``_orig`` /
         ``_thumb`` suffix. Match by filename to avoid touching the
         thumbnail siblings.
    """
    root = _Path(resources_root)
    now_ts = _time.time() if now is None else float(now)
    cutoff = now_ts - max(0, int(retention_days)) * 86400.0

    stats = {"scanned": 0, "deleted": 0, "deleted_bytes": 0, "errors": 0}
    if not root.exists():
        return stats

    # Sweep 1: new layout. Everything under originals/ is an original.
    originals_root = root / "originals"
    if originals_root.exists():
        for path in originals_root.rglob("*.jpg"):
            if not path.is_file():
                continue
            _maybe_delete(path, cutoff=cutoff, stats=stats)

    # Sweep 2: legacy layout. Only files matching `cap_*_orig.jpg`.
    # We deliberately walk the whole root (rather than just dates) because
    # legacy captures didn't have an `originals/` parent.
    for path in root.rglob("cap_*_orig.jpg"):
        # Skip the new-layout subtree to avoid double-counting.
        try:
            path.relative_to(originals_root)
            continue
        except ValueError:
            pass
        if not _LEGACY_ORIG_FILE_RE.match(path.name):
            continue
        _maybe_delete(path, cutoff=cutoff, stats=stats)

    return stats


def _maybe_delete(path: _Path, *, cutoff: float, stats: dict) -> None:
    """Delete `path` if its mtime is at or before `cutoff`. Updates stats in place."""
    stats["scanned"] += 1
    try:
        st = path.stat()
    except OSError:
        stats["errors"] += 1
        return
    if st.st_mtime > cutoff:
        return
    size = st.st_size
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("retention.unlink_failed path=%s err=%s", path, exc)
        stats["errors"] += 1
        return
    stats["deleted"] += 1
    stats["deleted_bytes"] += size


__all__ = ["select_expired", "purge_expired", "purge_orphan_originals"]
