"""Normalization helpers for photo library timeline ingestion."""
from __future__ import annotations

from typing import Any


def camera_display_name(make: str, model: str) -> str:
    """Build a human-readable camera name, deduplicating make from model."""
    make = make.strip()
    model = model.strip()
    if not make and not model:
        return ""
    if not make:
        return model
    if not model:
        return make
    # Many models already include the make name, e.g. "Apple iPhone 15 Pro"
    if model.lower().startswith(make.lower()):
        return model
    return f"{make} {model}"


def shooting_params_summary(item: dict[str, Any]) -> str:
    """Build a compact shooting parameters string like '50mm f/1.8 1/250s ISO400'."""
    parts: list[str] = []
    if item.get("focal_length"):
        parts.append(str(item["focal_length"]))
    if item.get("aperture"):
        parts.append(str(item["aperture"]))
    if item.get("exposure_time"):
        parts.append(str(item["exposure_time"]))
    if item.get("iso"):
        parts.append(f"ISO{item['iso']}")
    return " ".join(parts)


def build_session_entity_hints(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract entity hints from a session for L2 cognition.

    Sessions surface two kinds of first-class entities:

    * ``device``  鈥?the camera identity used during the session.
    * ``location`` 鈥?the geocoded place when GPS is available.
    """
    hints: list[dict[str, Any]] = []

    device_name = str(session.get("device_name") or "").strip()
    device_slug = str(session.get("device_slug") or "").strip()
    if device_name:
        hints.append({
            "mention_text": device_name,
            "entity_type": "device",
            "canonical_name_hint": device_slug or device_name,
        })

    lat = session.get("latitude")
    lon = session.get("longitude")
    location_name = str(session.get("location_name") or "")
    if lat is not None and lon is not None:
        coord_label = f"{lat:.4f}, {lon:.4f}"
        canonical = location_name or coord_label
        hints.append({
            "mention_text": canonical,
            "entity_type": "location",
            "canonical_name_hint": canonical,
            "attributes": {"latitude": lat, "longitude": lon},
        })
    return hints


def build_session_relation_candidates(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate relation candidates from a settled photo session.

    Two predicates are emitted:

    * ``user:self -OWNED_DEVICE-> device``  when device identity is known.
    * ``user:self -VISITED-> location``     when GPS / geocoded place is known.

    These are the long-lived facts a memory system actually needs about
    photos. Per-photo edges (``CREATED``, ``CAPTURED``, ``RELATED_TO``) are
    not emitted: photos are not graph nodes.

    ``RESIDED_IN`` is intentionally not produced here. Distinguishing a
    visit from a residence requires multi-week aggregation that belongs in
    a higher-level (L3) summarisation pass.
    """
    candidates: list[dict[str, Any]] = []
    observed_at = float(
        session.get("first_capture_ts")
        or session.get("last_capture_ts")
        or 0.0
    )

    device_slug = str(session.get("device_slug") or "").strip()
    device_name = str(session.get("device_name") or "").strip()
    if device_slug and device_name:
        candidates.append({
            "subject_id": "user:self",
            "subject_type": "user",
            "predicate": "OWNED_DEVICE",
            "object_id": f"device:{device_slug}",
            "object_type": "device",
            "confidence": 0.9,
            "observed_at": observed_at,
            "object_attributes": {
                "device_name": device_name,
                "source_kind": "exif",
            },
        })

    lat = session.get("latitude")
    lon = session.get("longitude")
    location_name = str(session.get("location_name") or "")
    if lat is not None and lon is not None:
        loc_id = location_name or f"{lat:.4f},{lon:.4f}"
        candidates.append({
            "subject_id": "user:self",
            "subject_type": "user",
            "predicate": "VISITED",
            "object_id": f"location:{loc_id}",
            "object_type": "location",
            "confidence": 0.85,
            "observed_at": observed_at,
            "object_attributes": {
                "latitude": lat,
                "longitude": lon,
                "location_name": location_name,
                "photo_count": int(session.get("photo_count") or 0),
                "source_kind": "gps",
            },
        })

    return candidates