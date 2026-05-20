"""Retention maintenance: delete expired originals and patch L1 metadata."""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


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


__all__ = ["select_expired", "purge_expired"]
