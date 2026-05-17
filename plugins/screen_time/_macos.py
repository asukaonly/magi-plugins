"""macOS foreground application reader via PyObjC.

Uses ``NSWorkspace.frontmostApplication`` for a thread-safe, non-blocking
snapshot of the currently active app. We deliberately avoid distributed
notifications because they require an NSRunLoop to be pumped on the calling
thread, which a headless Python sidecar cannot guarantee.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_AVAILABLE: bool | None = None


def _ensure_available() -> bool:
    """Check whether PyObjC AppKit bindings are importable on this host."""
    global _AVAILABLE
    if _AVAILABLE is not None:
        return _AVAILABLE
    try:
        import AppKit  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        logger.debug("PyObjC AppKit is not available; macOS screen-time reader disabled")
        _AVAILABLE = False
    else:
        _AVAILABLE = True
    return _AVAILABLE


def read_foreground() -> Optional[tuple[str, str]]:
    """Return ``(bundle_id, app_name)`` for the frontmost macOS app.

    Returns ``None`` when no frontmost app can be determined (e.g. during
    login screen) or when the bundle identifier is empty.
    """
    if not _ensure_available():
        return None
    try:
        from AppKit import NSWorkspace  # type: ignore[import-not-found]
    except ImportError:
        return None

    try:
        workspace = NSWorkspace.sharedWorkspace()
        application = workspace.frontmostApplication()
    except Exception:
        logger.debug("Failed to query NSWorkspace.frontmostApplication", exc_info=True)
        return None

    if application is None:
        return None

    bundle_id = _safe_string(application, "bundleIdentifier")
    if not bundle_id:
        return None

    app_name = _safe_string(application, "localizedName") or bundle_id
    return bundle_id, app_name


def _safe_string(target: object, attr: str) -> str:
    """Invoke an Objective-C zero-arg accessor and coerce the result to ``str``."""
    method = getattr(target, attr, None)
    if method is None:
        return ""
    try:
        value = method()
    except Exception:
        return ""
    if value is None:
        return ""
    return str(value).strip()
