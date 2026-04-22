"""Aggregate raw photo records into per-session L1 events.

A session represents a coherent shooting activity: same local date, same
device identity, same coarse geo cell. Adjacent sessions across midnight
that share device + location and fall within a small gap are merged.

Sessions are only considered finalized once their newest capture timestamp
is older than ``settle_window_seconds``. Unfinalized sessions are excluded
from emission so that a single L1 event per session ever lands in storage
(L1 store is INSERT OR IGNORE on idempotency_key).
"""
from __future__ import annotations

import time as _time
from datetime import datetime, timedelta
from typing import Any

from .normalizers import camera_display_name


# Geo grid for session bucketing: ~1.1 km. Tight enough to keep distinct
# venues separate, loose enough to absorb GPS drift while walking around.
_SESSION_GEO_CELL_DEGREES = 0.01

# Maximum representative photos per session (kept in domain_payload for UI).
_MAX_REPRESENTATIVE_PHOTOS = 20

# Same (device, geo) sessions whose chronological gap is below this are
# merged across midnight into the earlier session.
_CROSS_MIDNIGHT_GAP_SECONDS = 2 * 3600


def _device_slug(make: str, model: str) -> str:
    """Return a stable identifier for a device. Empty string when unknown."""
    name = camera_display_name(make, model)
    if not name:
        return ""
    return name.lower().replace(" ", "-")


def _geo_cell(lat: float | None, lon: float | None) -> str:
    if lat is None or lon is None:
        return "nogps"
    cell_lat = round(float(lat) / _SESSION_GEO_CELL_DEGREES) * _SESSION_GEO_CELL_DEGREES
    cell_lon = round(float(lon) / _SESSION_GEO_CELL_DEGREES) * _SESSION_GEO_CELL_DEGREES
    return f"{cell_lat:.2f},{cell_lon:.2f}"


def _local_date_str(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return "unknown"


def _weekday_index(ts: float) -> int:
    """Return ISO weekday index 0-6 (Mon=0). -1 when the timestamp is invalid.

    Stored as a language-neutral key so the sensor can localise it at
    output time via i18n templates.
    """
    try:
        return datetime.fromtimestamp(ts).weekday()
    except (OSError, OverflowError, ValueError):
        return -1


def _time_of_day(ts: float) -> str:
    try:
        h = datetime.fromtimestamp(ts).hour
    except (OSError, OverflowError, ValueError):
        return ""
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 18:
        return "afternoon"
    if 18 <= h < 22:
        return "evening"
    return "night"


def _session_key(date: str, device_slug: str, geo: str) -> str:
    return f"session:{date}:{device_slug or 'unknown-device'}:{geo}"


def aggregate_sessions(
    photos: list[dict[str, Any]],
    *,
    now_ts: float | None = None,
    settle_window_seconds: float = 4 * 3600,
) -> tuple[list[dict[str, Any]], float]:
    """Group photos into sessions and return the settled ones.

    Returns ``(sessions, max_settled_mtime)`` where ``max_settled_mtime`` is
    the largest file ``modified_at`` timestamp among emitted sessions, useful
    for cursor advancement.
    """
    if not photos:
        return [], 0.0

    now_ts = now_ts if now_ts is not None else _time.time()

    # Bucket photos by (date, device, geo).
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for p in photos:
        capture_ts = float(p.get("capture_timestamp") or p.get("modified_at") or 0.0)
        date = _local_date_str(capture_ts)
        device = _device_slug(
            str(p.get("camera_make", "")),
            str(p.get("camera_model", "")),
        )
        geo = _geo_cell(p.get("latitude"), p.get("longitude"))
        buckets.setdefault((date, device, geo), []).append(p)

    # Build raw sessions sorted within each bucket by capture time.
    raw_sessions: list[dict[str, Any]] = []
    for (date, device, geo), items in buckets.items():
        items.sort(key=lambda it: float(it.get("capture_timestamp") or 0.0))
        first_ts = float(items[0].get("capture_timestamp") or 0.0)
        last_ts = float(items[-1].get("capture_timestamp") or 0.0)
        raw_sessions.append({
            "date": date,
            "device_slug": device,
            "geo_cell": geo,
            "photos": items,
            "first_capture_ts": first_ts,
            "last_capture_ts": last_ts,
        })

    # Cross-midnight merge: sort by (device, geo, first_capture_ts) and fold
    # adjacent sessions with same (device, geo) and small gap.
    raw_sessions.sort(
        key=lambda s: (s["device_slug"], s["geo_cell"], s["first_capture_ts"])
    )
    merged: list[dict[str, Any]] = []
    for sess in raw_sessions:
        if merged:
            prev = merged[-1]
            same_id = (
                prev["device_slug"] == sess["device_slug"]
                and prev["geo_cell"] == sess["geo_cell"]
            )
            gap = sess["first_capture_ts"] - prev["last_capture_ts"]
            if same_id and 0 <= gap <= _CROSS_MIDNIGHT_GAP_SECONDS:
                prev["photos"].extend(sess["photos"])
                prev["last_capture_ts"] = sess["last_capture_ts"]
                continue
        merged.append(sess)

    settled: list[dict[str, Any]] = []
    max_settled_mtime = 0.0
    for sess in merged:
        # Settled = no new photo expected in this session within the window.
        if now_ts - sess["last_capture_ts"] < settle_window_seconds:
            continue
        first_photo = sess["photos"][0]
        device_name = camera_display_name(
            str(first_photo.get("camera_make", "")),
            str(first_photo.get("camera_model", "")),
        )
        # Pick a representative location_name (first non-empty).
        location_name = ""
        for p in sess["photos"]:
            ln = str(p.get("location_name") or "")
            if ln:
                location_name = ln
                break
        # Average GPS over the session for a single representative coord.
        gps_points = [
            (float(p["latitude"]), float(p["longitude"]))
            for p in sess["photos"]
            if p.get("latitude") is not None and p.get("longitude") is not None
        ]
        if gps_points:
            avg_lat = sum(p[0] for p in gps_points) / len(gps_points)
            avg_lon = sum(p[1] for p in gps_points) / len(gps_points)
        else:
            avg_lat = avg_lon = None

        burst_total = sum(int(p.get("burst_count") or 0) for p in sess["photos"])

        # Pick representative photos: hero (first) + evenly-spaced sampling.
        photos = sess["photos"]
        if len(photos) <= _MAX_REPRESENTATIVE_PHOTOS:
            reps = photos
        else:
            step = len(photos) / _MAX_REPRESENTATIVE_PHOTOS
            reps = [photos[int(i * step)] for i in range(_MAX_REPRESENTATIVE_PHOTOS)]
        rep_records = [
            {
                "path": str(p.get("path", "")),
                "asset_local_id": str(p.get("asset_local_id", "")),
                "capture_ts": float(p.get("capture_timestamp") or 0.0),
                "latitude": p.get("latitude"),
                "longitude": p.get("longitude"),
            }
            for p in reps
        ]

        max_session_mtime = max(
            float(p.get("modified_at") or 0.0) for p in sess["photos"]
        )
        max_settled_mtime = max(max_settled_mtime, max_session_mtime)

        # Recompute date label from the merged session's first capture, since
        # cross-midnight merging may have shifted the natural date.
        canonical_date = _local_date_str(sess["first_capture_ts"])

        settled.append({
            "session_key": _session_key(
                canonical_date, sess["device_slug"], sess["geo_cell"]
            ),
            "date": canonical_date,
            "weekday_index": _weekday_index(sess["first_capture_ts"]),
            "time_of_day": _time_of_day(sess["first_capture_ts"]),
            "device_slug": sess["device_slug"],
            "device_name": device_name,
            "location_name": location_name,
            "latitude": avg_lat,
            "longitude": avg_lon,
            "geo_cell": sess["geo_cell"],
            "photo_count": len(sess["photos"]),
            "burst_total": burst_total,
            "first_capture_ts": sess["first_capture_ts"],
            "last_capture_ts": sess["last_capture_ts"],
            "max_modified_at": max_session_mtime,
            "representative_photos": rep_records,
        })

    return settled, max_settled_mtime
