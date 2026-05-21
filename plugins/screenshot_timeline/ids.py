"""ULID-based identity helpers for screenshot timeline events."""
from __future__ import annotations

import os
import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid(now: float | None = None) -> str:
    ts_ms = int((now if now is not None else time.time()) * 1000)
    time_part = _encode(ts_ms, 10)
    rand_part = "".join(_CROCKFORD[b % 32] for b in secrets.token_bytes(16))[:16]
    return time_part + rand_part


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def new_capture_id(now: float | None = None) -> str:
    """Generate a stable ULID-style capture id with a `cap_` prefix."""
    return "cap_" + _ulid(now=now)


def burst_source_item_id(
    *,
    start_unix: float,
    app_bundle: str,
    window_id_hash: str,
) -> str:
    """Compose a burst's deterministic source-side item identity."""
    date = time.strftime("%Y%m%d", time.gmtime(start_unix))
    return f"{date}_{int(start_unix)}_{app_bundle}_{window_id_hash}"


def short_window_hash(window_title: str, app_bundle: str) -> str:
    """Stable short hash for keying windows when AX window id is unavailable."""
    h = 0
    for ch in (app_bundle + "|" + window_title):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return f"{h:08x}"[:4]


__all__ = ["new_capture_id", "burst_source_item_id", "short_window_hash"]
