"""Lockscreen detection via Quartz CGSessionCopyCurrentDictionary."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def is_screen_locked() -> bool:
    """Return True if the current macOS session is locked.

    Uses `CGSessionCopyCurrentDictionary` and looks at the
    `CGSSessionScreenIsLocked` field. Returns False on any failure
    (missing framework, no session, unexpected dict shape) so capture
    is not falsely suppressed.
    """
    try:
        # Quartz exposes the session dictionary helper at module top-level
        from Quartz import CGSessionCopyCurrentDictionary  # type: ignore
    except Exception:  # noqa: BLE001
        logger.debug("screen_lock.import_failed — assuming unlocked")
        return False

    try:
        session = CGSessionCopyCurrentDictionary()
    except Exception:  # noqa: BLE001
        logger.debug("screen_lock.query_failed — assuming unlocked")
        return False

    if not session:
        return False
    try:
        # CFDictionary supports dict-like access via pyobjc
        value = session.get("CGSSessionScreenIsLocked")
    except Exception:  # noqa: BLE001
        return False
    if value is None:
        return False
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return bool(value)


__all__ = ["is_screen_locked"]
