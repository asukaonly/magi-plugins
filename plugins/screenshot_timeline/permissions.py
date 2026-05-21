"""macOS permission probes — delegate to the Swift helper subprocess.

Background: macOS TCC keys per-binary. The Python sidecar and the Swift helper
are separate binaries with separate TCC entries. We delegate probes to the
helper because IT is the binary that actually makes ScreenCaptureKit /
VNRecognizeTextRequest calls, so its TCC entry is the one that matters.

Probes spawn a one-shot helper subprocess; they do NOT reuse the long-lived
sensor helper because read_settings_resource() runs even when the sensor is
disabled (and therefore not running).
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Literal, Optional

logger = logging.getLogger(__name__)

PermissionStatus = Literal["granted", "denied", "not_determined", "unknown"]

_PROBE_TIMEOUT_SECONDS = 5.0


def _helper_binary_path() -> Optional[Path]:
    """Locate the bundled Swift helper binary."""
    plugin_dir = Path(__file__).resolve().parent
    candidate = plugin_dir / "bin" / "magi-vision-helper"
    if candidate.exists() and candidate.is_file():
        return candidate
    return None


def _probe_via_helper(op: str) -> PermissionStatus:
    """Run a single probe op through a fresh helper subprocess."""
    helper = _helper_binary_path()
    if helper is None:
        logger.warning("permissions.helper_not_found op=%s", op)
        return "unknown"

    payload = json.dumps({"id": "p", "op": op}) + "\n"
    shutdown = json.dumps({"id": "s", "op": "shutdown"}) + "\n"

    try:
        proc = subprocess.run(
            [str(helper)],
            input=payload + shutdown,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("permissions.helper_invoke_failed op=%s err=%r", op, exc)
        return "unknown"

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            resp = json.loads(line)
        except json.JSONDecodeError:
            continue
        if resp.get("id") != "p":
            continue
        if not resp.get("ok"):
            err = (resp.get("error") or {}).get("code", "")
            logger.warning("permissions.helper_error op=%s code=%s", op, err)
            return "unknown"
        raw = resp.get("permission_status")
        if raw in ("granted", "denied", "not_determined", "unknown"):
            return raw  # type: ignore[return-value]
        logger.warning("permissions.helper_unexpected_status op=%s value=%r", op, raw)
        return "unknown"

    logger.warning(
        "permissions.helper_no_response op=%s rc=%s stdout=%r stderr=%r",
        op, proc.returncode, proc.stdout[:200], proc.stderr[:200],
    )
    return "unknown"


def screen_recording_status() -> PermissionStatus:
    """Check Screen Recording permission as seen by the helper binary."""
    return _probe_via_helper("probe_screen_recording")


def request_screen_recording() -> PermissionStatus:
    """Trigger the Screen Recording permission prompt for the helper binary."""
    return _probe_via_helper("request_screen_recording")


def accessibility_status() -> PermissionStatus:
    """Check Accessibility permission as seen by the helper binary."""
    return _probe_via_helper("probe_accessibility")


def request_accessibility() -> PermissionStatus:
    """Trigger the Accessibility permission prompt for the helper binary."""
    return _probe_via_helper("request_accessibility")


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
