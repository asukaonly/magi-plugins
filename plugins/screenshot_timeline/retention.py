"""Retention maintenance for connection-owned original screenshots."""
from __future__ import annotations

import logging
import time as _time
from pathlib import Path as _Path

logger = logging.getLogger(__name__)

def purge_orphan_originals(
    resources_root: str | _Path,
    *,
    retention_days: int,
    now: float | None = None,
) -> dict:
    """Walk the resources tree and delete expired originals.

    Returns a stats dict: {scanned: int, deleted: int, deleted_bytes: int, errors: int}.

    Thumbnails are never touched.

    Only the connection-owned ``originals/`` tree is scanned.
    """
    root = _Path(resources_root)
    now_ts = _time.time() if now is None else float(now)
    cutoff = now_ts - max(0, int(retention_days)) * 86400.0

    stats = {"scanned": 0, "deleted": 0, "deleted_bytes": 0, "errors": 0}
    if not root.exists():
        return stats

    # Every file under originals/ belongs to this connection.
    originals_root = root / "originals"
    if originals_root.exists():
        for path in originals_root.rglob("*.jpg"):
            if not path.is_file():
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


__all__ = ["purge_orphan_originals"]
