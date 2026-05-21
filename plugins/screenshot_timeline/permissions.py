"""macOS permission probes and request helpers for the screenshot_timeline plugin."""
from __future__ import annotations

import logging
import os
import sys
from typing import Literal

logger = logging.getLogger(__name__)

PermissionStatus = Literal["granted", "denied", "not_determined", "unknown"]


def _process_diagnostics() -> str:
    """Return a one-line summary of which binary is actually making the TCC call.

    macOS Privacy is keyed per-binary, so when the result is unexpectedly 'denied'
    you need to know exactly what path/PID to add in System Settings.
    """
    try:
        pid = os.getpid()
        ppid = os.getppid()
        executable = sys.executable or "<unknown>"
        argv0 = sys.argv[0] if sys.argv else "<no argv>"
        return f"pid={pid} ppid={ppid} executable={executable} argv0={argv0}"
    except Exception:
        return "(diagnostics unavailable)"


def screen_recording_status() -> PermissionStatus:
    """Check Screen Recording permission without triggering a prompt."""
    diag = _process_diagnostics()
    try:
        from Quartz import CGPreflightScreenCaptureAccess  # type: ignore
    except Exception as exc:
        logger.warning("permissions.screen_recording.import_failed %s err=%r", diag, exc)
        return "unknown"
    try:
        raw = CGPreflightScreenCaptureAccess()
        status: PermissionStatus = "granted" if raw else "denied"
        logger.warning(
            "permissions.screen_recording.probe status=%s raw=%r %s",
            status, raw, diag,
        )
        return status
    except Exception as exc:
        logger.warning("permissions.screen_recording.query_failed %s err=%r", diag, exc)
        return "unknown"


def request_screen_recording() -> PermissionStatus:
    """Trigger the Screen Recording permission prompt (macOS shows it once per binary).

    Returns the immediate status. The actual user decision happens out-of-band; you should
    poll `screen_recording_status` afterwards.
    """
    diag = _process_diagnostics()
    try:
        from Quartz import CGRequestScreenCaptureAccess  # type: ignore
    except Exception as exc:
        logger.warning("permissions.screen_recording_request.import_failed %s err=%r", diag, exc)
        return "unknown"
    try:
        result = CGRequestScreenCaptureAccess()
        logger.warning(
            "permissions.screen_recording_request.invoked result=%r %s",
            result, diag,
        )
    except Exception as exc:
        logger.warning("permissions.screen_recording_request.failed %s err=%r", diag, exc)
    return screen_recording_status()


def accessibility_status() -> PermissionStatus:
    """Check Accessibility permission without triggering a prompt."""
    diag = _process_diagnostics()
    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore
    except Exception as exc:
        logger.warning("permissions.accessibility.import_failed %s err=%r", diag, exc)
        return "unknown"
    try:
        raw = AXIsProcessTrusted()
        status: PermissionStatus = "granted" if raw else "denied"
        logger.warning(
            "permissions.accessibility.probe status=%s raw=%r %s",
            status, raw, diag,
        )
        return status
    except Exception as exc:
        logger.warning("permissions.accessibility.query_failed %s err=%r", diag, exc)
        return "unknown"


def request_accessibility() -> PermissionStatus:
    """Trigger the Accessibility permission prompt."""
    diag = _process_diagnostics()
    try:
        from ApplicationServices import (  # type: ignore
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )
    except Exception as exc:
        logger.warning("permissions.accessibility_request.import_failed %s err=%r", diag, exc)
        return "unknown"
    try:
        options = {kAXTrustedCheckOptionPrompt: True}
        result = AXIsProcessTrustedWithOptions(options)
        logger.warning(
            "permissions.accessibility_request.invoked result=%r %s",
            result, diag,
        )
    except Exception as exc:
        logger.warning("permissions.accessibility_request.failed %s err=%r", diag, exc)
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
