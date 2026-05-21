"""macOS permission probes and request helpers for the screenshot_timeline plugin."""
from __future__ import annotations

import logging
from typing import Literal

logger = logging.getLogger(__name__)

PermissionStatus = Literal["granted", "denied", "not_determined", "unknown"]


def screen_recording_status() -> PermissionStatus:
    """Check Screen Recording permission without triggering a prompt."""
    try:
        from Quartz import CGPreflightScreenCaptureAccess  # type: ignore
    except Exception:
        logger.debug("permissions.screen_recording.import_failed")
        return "unknown"
    try:
        return "granted" if CGPreflightScreenCaptureAccess() else "denied"
    except Exception:
        logger.debug("permissions.screen_recording.query_failed")
        return "unknown"


def request_screen_recording() -> PermissionStatus:
    """Trigger the Screen Recording permission prompt (macOS shows it once per binary).

    Returns the immediate status. The actual user decision happens out-of-band; you should
    poll `screen_recording_status` afterwards.
    """
    try:
        from Quartz import CGRequestScreenCaptureAccess  # type: ignore
    except Exception:
        logger.debug("permissions.screen_recording_request.import_failed")
        return "unknown"
    try:
        CGRequestScreenCaptureAccess()
    except Exception:
        logger.debug("permissions.screen_recording_request.failed")
    return screen_recording_status()


def accessibility_status() -> PermissionStatus:
    """Check Accessibility permission without triggering a prompt."""
    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore
    except Exception:
        logger.debug("permissions.accessibility.import_failed")
        return "unknown"
    try:
        return "granted" if AXIsProcessTrusted() else "denied"
    except Exception:
        return "unknown"


def request_accessibility() -> PermissionStatus:
    """Trigger the Accessibility permission prompt."""
    try:
        from ApplicationServices import (  # type: ignore
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )
    except Exception:
        logger.debug("permissions.accessibility_request.import_failed")
        return "unknown"
    try:
        options = {kAXTrustedCheckOptionPrompt: True}
        AXIsProcessTrustedWithOptions(options)
    except Exception:
        logger.debug("permissions.accessibility_request.failed")
    return accessibility_status()


def all_statuses() -> dict[str, PermissionStatus]:
    return {
        "screen_recording": screen_recording_status(),
        "accessibility": accessibility_status(),
    }


__all__ = [
    "PermissionStatus",
    "screen_recording_status",
    "request_screen_recording",
    "accessibility_status",
    "request_accessibility",
    "all_statuses",
]
