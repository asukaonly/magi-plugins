"""Identity helpers for screenshot timeline events.

`new_capture_id()` returns human-readable timestamp-prefixed ids like
`20260522T134523_456789_K3FW`. These have the same lexicographic-equals-
chronological ordering as a ULID but tell the user when a screenshot was
taken at a glance — useful in Finder, in logs, and when debugging
retention.

The id is also the on-disk filename stem: `<capture_id>.jpg` in both
`originals/` and `thumbnails/`. Callers should NOT append `_orig` /
`_thumb` suffixes anymore.
"""
from __future__ import annotations

import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def new_capture_id(now: float | None = None) -> str:
    """Generate a timestamp-prefixed capture id.

    Format: ``YYYYMMDDTHHMMSS_<microseconds>_<4char-random>``

    The first three components fully order ids chronologically when
    compared lexicographically (each component is fixed-width zero-
    padded). The 4-character random tail catches the rare case of two
    capture triggers firing within the same microsecond (e.g. the
    NSWorkspace observer and the interval timer racing on an app
    switch).
    """
    when = now if now is not None else time.time()
    seconds = int(when)
    micros = int((when - seconds) * 1_000_000)
    micros = max(0, min(999_999, micros))
    local = time.localtime(seconds)
    prefix = time.strftime("%Y%m%dT%H%M%S", local)
    tail = "".join(_CROCKFORD[b % 32] for b in secrets.token_bytes(4))[:4]
    return f"{prefix}_{micros:06d}_{tail}"


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
