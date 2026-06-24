"""Normalization helpers for photo library timeline ingestion."""

from __future__ import annotations

from typing import Any

_LOCATION_QUERY_ALIASES = {
    "tokyo": "东京",
    "japan": "日本",
}


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


def build_session_retrieval_terms(session: dict[str, Any]) -> list[str]:
    """Build compact search terms for photo-session retrieval and embeddings."""
    terms = ["photo_library", "session"]
    if session.get("latitude") is not None or session.get("location_name"):
        terms.append("geo")

    place_name = str(
        session.get("apple_photos_place_name") or session.get("location_name") or ""
    ).strip()
    place_address = str(session.get("apple_photos_place_address") or "").strip()
    _append_unique(terms, place_name)
    _append_unique(terms, place_address)

    location_text = " ".join(value for value in (place_name, place_address) if value)
    location_text_lower = location_text.lower()
    for token, alias in _LOCATION_QUERY_ALIASES.items():
        if token in location_text_lower:
            _append_unique(terms, alias)

    return terms


def build_session_source_facets(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Build exact source facets for structured L1 recall."""
    facets: list[dict[str, Any]] = []

    photo_count = _int_value(session.get("photo_count"))
    if photo_count is not None:
        facets.append({"name": "photo.count", "numeric": photo_count})

    device_name = str(session.get("device_name") or "").strip()
    if device_name:
        facets.append({"name": "photo.device", "text": device_name})

    for key, facet_name in (
        ("location_name", "photo.location_name"),
        ("apple_photos_place_name", "photo.location_name"),
        ("apple_photos_place_address", "photo.location_alias"),
    ):
        value = str(session.get(key) or "").strip()
        if value:
            facets.append({"name": facet_name, "text": value})

    for key, facet_name in (
        ("latitude", "photo.latitude"),
        ("longitude", "photo.longitude"),
    ):
        numeric = _float_value(session.get(key))
        if numeric is not None:
            facets.append({"name": facet_name, "numeric": numeric})

    for photo in session.get("representative_photos") or []:
        if not isinstance(photo, dict):
            continue
        for key, facet_name in (
            ("location_name", "photo.location_name"),
            ("apple_photos_place_name", "photo.location_name"),
            ("apple_photos_place_address", "photo.location_alias"),
            ("place_address", "photo.location_alias"),
            ("address", "photo.location_alias"),
            ("asset_local_id", "photo.asset_id"),
            ("local_identifier", "photo.asset_id"),
        ):
            value = str(photo.get(key) or "").strip()
            if value:
                facets.append({"name": facet_name, "text": value})
        for key, facet_name in (
            ("latitude", "photo.latitude"),
            ("longitude", "photo.longitude"),
        ):
            numeric = _float_value(photo.get(key))
            if numeric is not None:
                facets.append({"name": facet_name, "numeric": numeric})

    for term in build_session_retrieval_terms(session):
        facets.append({"name": "photo.retrieval_term", "text": term})

    return _dedupe_facets(facets)


def _append_unique(values: list[str], value: str) -> None:
    normalized = str(value or "").strip()
    if not normalized:
        return
    seen = {item.lower() for item in values}
    if normalized.lower() not in seen:
        values.append(normalized)


def _dedupe_facets(facets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    unique: list[dict[str, Any]] = []
    for facet in facets:
        key = (
            facet.get("name"),
            str(facet.get("text") or "").casefold(),
            facet.get("numeric"),
            facet.get("timestamp"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(facet)
    return unique


def _int_value(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_value(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
        hints.append(
            {
                "mention_text": device_name,
                "entity_type": "device",
                "canonical_name_hint": device_slug or device_name,
            }
        )

    lat = session.get("latitude")
    lon = session.get("longitude")
    location_name = str(session.get("location_name") or "")
    if lat is not None and lon is not None:
        coord_label = f"{lat:.4f}, {lon:.4f}"
        canonical = location_name or coord_label
        hints.append(
            {
                "mention_text": canonical,
                "entity_type": "place",
                "canonical_name_hint": canonical,
                "attributes": {"latitude": lat, "longitude": lon},
            }
        )
    return hints


def build_session_relation_candidates(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate relation candidates from a settled photo session.

    Two predicates are emitted:

    * ``user:self -OWNS-> device``  when device identity is known.
    * ``user:self -VISITED-> location``     when GPS / geocoded place is known.

    These are the long-lived facts a memory system actually needs about
    photos. Per-photo edges (``CREATED``, ``CAPTURED``, ``RELATED_TO``) are
    not emitted: photos are not graph nodes.

    ``RESIDED_IN`` is intentionally not produced here. Distinguishing a
    visit from a residence requires multi-week aggregation that belongs in
    a higher-level (L3) summarisation pass.
    """
    candidates: list[dict[str, Any]] = []
    observed_at = float(session.get("first_capture_ts") or session.get("last_capture_ts") or 0.0)

    device_slug = str(session.get("device_slug") or "").strip()
    device_name = str(session.get("device_name") or "").strip()
    if device_slug and device_name:
        candidates.append(
            {
                "subject_id": "user:self",
                "subject_type": "user",
                "predicate": "OWNS",
                "object_id": f"device:{device_slug}",
                "object_type": "device",
                "confidence": 0.9,
                "observed_at": observed_at,
                "object_attributes": {
                    "device_name": device_name,
                    "source_kind": "exif",
                },
            }
        )

    lat = session.get("latitude")
    lon = session.get("longitude")
    location_name = str(session.get("location_name") or "")
    if lat is not None and lon is not None:
        loc_id = location_name or f"{lat:.4f},{lon:.4f}"
        candidates.append(
            {
                "subject_id": "user:self",
                "subject_type": "user",
                "predicate": "VISITED",
                "object_id": f"location:{loc_id}",
                "object_type": "place",
                "confidence": 0.85,
                "observed_at": observed_at,
                "object_attributes": {
                    "latitude": lat,
                    "longitude": lon,
                    "location_name": location_name,
                    "photo_count": int(session.get("photo_count") or 0),
                    "source_kind": "gps",
                },
            }
        )

    return candidates


def build_session_fact_hints(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate source-owned L2 graph hints from a settled photo session."""

    hints: list[dict[str, Any]] = []
    observed_at = float(session.get("first_capture_ts") or session.get("last_capture_ts") or 0.0)

    device_slug = str(session.get("device_slug") or "").strip()
    device_name = str(session.get("device_name") or "").strip()
    if device_slug and device_name:
        hints.append(
            {
                "subject_ref": "user:self",
                "subject_type": "user",
                "predicate": "OWNS",
                "object_ref": f"hardware:{device_slug}",
                "object_type": "hardware",
                "fact_kind": "interaction_evidence",
                "origin_mode": "source_structured",
                "confidence": 0.9,
                "observed_at": observed_at,
                "attributes": {
                    "device_name": device_name,
                    "source_kind": "exif",
                },
            }
        )

    lat = session.get("latitude")
    lon = session.get("longitude")
    location_name = str(session.get("location_name") or "")
    if lat is not None and lon is not None:
        loc_id = location_name or f"{lat:.4f},{lon:.4f}"
        hints.append(
            {
                "subject_ref": "user:self",
                "subject_type": "user",
                "predicate": "VISITED",
                "object_ref": f"place:{loc_id}",
                "object_type": "place",
                "fact_kind": "interaction_evidence",
                "origin_mode": "source_structured",
                "confidence": 0.85,
                "observed_at": observed_at,
                "attributes": {
                    "latitude": lat,
                    "longitude": lon,
                    "location_name": location_name,
                    "photo_count": int(session.get("photo_count") or 0),
                    "source_kind": "gps",
                },
            }
        )

    return hints
