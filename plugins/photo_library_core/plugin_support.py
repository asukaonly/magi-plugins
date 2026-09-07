"""Shared plugin contract builders for independently installed photo sources."""
from __future__ import annotations

from collections import Counter
from typing import Any

from magi_plugin_sdk import (
    PluginManifest,
    ExtractionProfileSpec,
    SourceSpec,
)

from .source import PhotoLibraryTimelineSource

CAPABILITY_ID = "photo_library"
CAPABILITY_DISPLAY_NAME = "Photo Library"
CAPABILITY_DESCRIPTION = "Manage installed photo sources that feed the timeline."
APPLE_PHOTOS_SOURCE_TYPE = "photo_library_apple_photos"
DIRECTORY_SOURCE_TYPE = "photo_library_directory"
PHOTO_LIBRARY_L2_ENTITY_TYPES = ["hardware", "place"]
PHOTO_LIBRARY_L2_PREDICATES = ["OWNS", "VISITED"]

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "sync_mode": "interval",
    "sync_interval_minutes": 60,
    "source_paths": [],
    "exclude_patterns": ["**/thumbnails", "**/.cache", "**/Thumbs.db", "**/@eaDir"],
    "photos_library_path": "",
    "max_items_per_sync": 200,
    "analysis_features": ["exif"],
    "settle_window_hours": 4,
}


def source_defaults(source_mode: str) -> dict[str, Any]:
    defaults = dict(DEFAULT_SETTINGS)
    defaults["source_mode"] = source_mode
    return defaults


def build_extraction_profile(source_type: str) -> ExtractionProfileSpec:
    return ExtractionProfileSpec(
        profile_id=f"source.{source_type}",
        source_types=[source_type],
        allowed_entity_types=PHOTO_LIBRARY_L2_ENTITY_TYPES,
        allowed_predicates=PHOTO_LIBRARY_L2_PREDICATES,
        structured_allowed_entity_types=PHOTO_LIBRARY_L2_ENTITY_TYPES,
        structured_allowed_predicates=PHOTO_LIBRARY_L2_PREDICATES,
        allowed_assertion_families=[],
        allow_graph=True,
        allow_assertion=False,
        derived_assertion_specs=[],
        extraction_instructions=(
            "These events are settled photo sessions. The source already provides "
            "high-confidence structured hints for camera hardware and visited places.\n"
            "Only keep graph facts that connect the user to owned hardware or visited "
            "places. Do not infer preferences, identity, residence, routines, mood, "
            "or long-term conclusions from a photo session."
        ),
    )


def build_source_registration(
    plugin_settings: dict[str, Any],
    *,
    manifest: PluginManifest,
    source_type: str,
    entry_id: str,
    display_name: str,
    description: str,
    source_mode: str,
    entry_order: int,
    metadata_extra: dict[str, Any] | None = None,
) -> tuple[str, object, SourceSpec]:
    sources_settings = plugin_settings.get("sources", {})
    sources_payload = sources_settings if isinstance(sources_settings, dict) else {}
    settings = dict(sources_payload.get(source_type, {}))
    defaults = source_defaults(source_mode)
    raw_paths = settings.get("source_paths")
    source_paths = [str(path) for path in raw_paths if path] if isinstance(raw_paths, list) else []
    raw_excludes = settings.get("exclude_patterns")
    exclude_patterns = (
        [str(pattern) for pattern in raw_excludes if pattern]
        if isinstance(raw_excludes, list)
        else []
    )
    source_id = f"timeline.photo_library.{entry_id}"
    source = PhotoLibraryTimelineSource(
        source_id=source_id,
        source_type=source_type,
        display_name=display_name,
        source_paths=source_paths,
        source_mode=source_mode,
        photos_library_path=str(
            settings.get("photos_library_path", defaults["photos_library_path"])
        ),
        max_items_per_sync=int(
            settings.get("max_items_per_sync", defaults["max_items_per_sync"])
        ),
        analysis_features=list(
            settings.get("analysis_features", defaults["analysis_features"])
        ),
        exclude_patterns=exclude_patterns,
        settle_window_seconds=float(
            settings.get("settle_window_hours", defaults["settle_window_hours"])
        )
        * 3600.0,
    )
    metadata = {
        "source_type": source_type,
        "default_settings": {
            field.key.rsplit(".", 1)[-1]: field.model_copy(deep=True).default
            for field in manifest.settings_fields
            if field.key.startswith("sources.") and field.type != "secret"
        },
        "activation_flow": manifest.activation_flow.model_dump() if manifest.activation_flow is not None else None,
        "capability_id": CAPABILITY_ID,
        "capability_display_name": CAPABILITY_DISPLAY_NAME,
        "capability_description": CAPABILITY_DESCRIPTION,
        "entry_id": entry_id,
        "entry_display_name": display_name,
        "entry_description": description,
        "entry_order": entry_order,
        **(metadata_extra or {}),
    }
    return (
        source_id,
        source,
        SourceSpec(
            source_id=source_id,
            display_name=display_name,
            description=description,
            domain="timeline",
            surface="timeline",
            sync_mode=str(settings.get("sync_mode", defaults["sync_mode"])),
            polling_mode=getattr(source, "polling_mode", "interval"),
            fields=[
                field.model_copy(deep=True)
                for field in sorted(manifest.settings_fields, key=lambda field: field.order)
                if field.surface == "timeline" and field.section != "activation"
            ],
            metadata=metadata,
        ),
    )


def build_recall_artifacts(
    *,
    source_type: str,
    events: list[dict[str, Any]],
    expected_source_type: str,
    resolver_tool: str,
) -> dict[str, object] | None:
    if source_type != expected_source_type or not events:
        return None
    asset_refs: list[dict[str, Any]] = []
    for event in events:
        asset_refs.extend(
            _build_recall_asset_refs(
                event,
                expected_source_type=expected_source_type,
                resolver_tool=resolver_tool,
            )
        )
    return {"asset_refs": asset_refs} if asset_refs else None


def _build_recall_asset_refs(
    event: dict[str, Any],
    *,
    expected_source_type: str,
    resolver_tool: str,
) -> list[dict[str, Any]]:
    metadata = event.get("metadata_json") if isinstance(event.get("metadata_json"), dict) else {}
    activity_snapshot = (
        metadata.get("activity_snapshot")
        if isinstance(metadata.get("activity_snapshot"), dict)
        else {}
    )
    event_source_type = str(activity_snapshot.get("source_type") or "").strip()
    if event_source_type != expected_source_type:
        return []
    provenance = (
        activity_snapshot.get("provenance")
        if isinstance(activity_snapshot.get("provenance"), dict)
        else {}
    )
    representative_photos = metadata.get("representative_photos")
    if not isinstance(representative_photos, list):
        return []
    title = str(activity_snapshot.get("title") or "").strip() or None
    location_name = str(provenance.get("location_name") or "").strip() or None
    device_name = str(provenance.get("device_name") or "").strip() or None
    session_source_item_id = str(metadata.get("source_object_id") or "").strip()
    connection_id = str(metadata.get("source_connection_id") or "").strip()
    if not session_source_item_id or not connection_id:
        return []
    event_id = str(event.get("event_id") or "").strip() or None
    occurred_at = event.get("timestamp") or event.get("created_at")
    refs: list[dict[str, Any]] = []
    for index, item in enumerate(representative_photos):
        if not isinstance(item, dict):
            continue
        asset_ref_id = str(item.get("asset_local_id") or "").strip()
        if not asset_ref_id:
            continue
        attributes: dict[str, Any] = {"representative_index": index + 1}
        if session_source_item_id is not None:
            attributes["session_source_item_id"] = session_source_item_id
        if location_name is not None:
            attributes["location_name"] = location_name
        if device_name is not None:
            attributes["device_name"] = device_name
        if item.get("latitude") is not None:
            attributes["latitude"] = item.get("latitude")
        if item.get("longitude") is not None:
            attributes["longitude"] = item.get("longitude")
        ref = {
            "asset_ref_id": asset_ref_id,
            "kind": "image",
            "event_id": event_id,
            "source_type": event_source_type,
            "source_item_id": asset_ref_id,
            "connection_id": connection_id,
            "display_name": title,
            "captured_at": item.get("capture_ts")
            or provenance.get("first_capture_ts")
            or occurred_at,
            "occurred_at": occurred_at,
            "resolver_tool": resolver_tool,
            "attributes": attributes,
        }
        refs.append(
            {key: value for key, value in ref.items() if value not in (None, "", [], {})}
        )
    return refs


def build_temporal_summary_features(
    *,
    source_type: str,
    events: list[dict[str, Any]],
    expected_source_type: str,
    budget: object | None,
) -> dict[str, object] | None:
    if source_type != expected_source_type or not events:
        return None
    device_counter: Counter[str] = Counter()
    location_counter: Counter[str] = Counter()
    photo_total = 0
    gps_session_count = 0
    days_active: set[str] = set()
    for event in events:
        metadata = event.get("metadata_json")
        if not isinstance(metadata, dict):
            continue
        activity_snapshot = metadata.get("activity_snapshot")
        if not isinstance(activity_snapshot, dict):
            continue
        provenance = activity_snapshot.get("provenance")
        if not isinstance(provenance, dict):
            continue
        device = str(provenance.get("device_name") or "").strip()
        if device:
            device_counter[device] += 1
        location = str(provenance.get("location_name") or "").strip()
        if location:
            location_counter[location] += 1
        if provenance.get("latitude") is not None:
            gps_session_count += 1
        try:
            photo_total += int(provenance.get("photo_count") or 0)
        except (TypeError, ValueError):
            pass
        date = str(provenance.get("date") or "")
        if date:
            days_active.add(date)
    top_devices = [
        {"device": device, "session_count": count}
        for device, count in device_counter.most_common(3)
    ]
    top_locations = [
        {"location": location, "session_count": count}
        for location, count in location_counter.most_common(5)
    ]
    summary_lines = [
        f"{len(events)} photo sessions across {len(days_active)} days, "
        f"{photo_total} photos in total."
    ]
    if top_devices:
        summary_lines.append(
            f"Most active devices: {' and '.join(item['device'] for item in top_devices[:2])}."
        )
    if top_locations:
        summary_lines.append(
            f"Visited: {', '.join(item['location'] for item in top_locations[:3])}."
        )
    covered_event_count = len(events)
    total_event_count = _budget_int(budget, "total_event_count", covered_event_count)
    omitted_event_count = max(0, total_event_count - covered_event_count)
    if omitted_event_count > 0:
        summary_lines.append(
            f"Photo feature coverage used {covered_event_count} representative sessions; "
            f"{omitted_event_count} additional sessions were compacted."
        )
    return {
        "feature_type": "photo_library",
        "session_count": covered_event_count,
        "total_event_count": total_event_count,
        "covered_event_count": covered_event_count,
        "omitted_event_count": omitted_event_count,
        "coverage_ratio": (
            covered_event_count / total_event_count if total_event_count else None
        ),
        "photo_total": photo_total,
        "active_days": len(days_active),
        "devices": top_devices,
        "locations": top_locations,
        "gps_session_count": gps_session_count,
        "summary_lines": summary_lines,
    }


def _budget_int(budget: object | None, key: str, default: int) -> int:
    if budget is None:
        return int(default)
    raw = budget.get(key, default) if isinstance(budget, dict) else getattr(budget, key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)
