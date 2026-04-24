"""Photo library timeline plugin."""
from __future__ import annotations

from collections import Counter
from typing import Any

from magi_plugin_sdk import ExtensionFieldOption, ExtensionFieldSpec, Plugin, SensorSpec
from .photo_tools import build_photo_library_tool_classes
from .sensor import PhotoLibraryTimelineSensor


DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "sync_mode": "manual",
    "sync_interval_minutes": 60,
    "source_paths": [],
    "exclude_patterns": ["**/thumbnails", "**/.cache", "**/Thumbs.db", "**/@eaDir"],
    "max_items_per_sync": 200,
    "analysis_features": ["exif"],
    "settle_window_hours": 4,
}


def _fields(prefix: str) -> list[ExtensionFieldSpec]:
    return [
        ExtensionFieldSpec(
            key=f"{prefix}.enabled",
            type="switch",
            label="Enable",
            description="Whether photo library sync is active.",
            default=False,
            section="general",
            surface="timeline",
            order=10,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.source_paths",
            type="path",
            label="Photo Directories",
            description="Local directories containing photos to scan. Add one or more paths.",
            default=[],
            required=True,
            section="general",
            surface="timeline",
            order=15,
            placeholder="/path/to/photos",
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.exclude_patterns",
            type="tags",
            label="Exclude Patterns",
            description="Glob patterns for directories or files to skip (e.g. thumbnails, .cache).",
            default=["**/thumbnails", "**/.cache", "**/Thumbs.db", "**/@eaDir"],
            section="general",
            surface="timeline",
            order=16,
            placeholder="**/thumbnails",
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sync_mode",
            type="select",
            label="Sync Mode",
            description="How photo library should be synchronized.",
            default="manual",
            options=[
                ExtensionFieldOption(label="Manual", value="manual"),
                ExtensionFieldOption(label="Interval", value="interval"),
            ],
            section="general",
            surface="timeline",
            order=20,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sync_interval_minutes",
            type="number",
            label="Sync Interval (minutes)",
            description="Polling interval used for interval-based sync.",
            default=60,
            section="general",
            surface="timeline",
            order=30,
            depends_on_key=f"{prefix}.sync_mode",
            depends_on_values=["interval"],
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.max_items_per_sync",
            type="number",
            label="Max Items Per Sync",
            description="Maximum number of photos to process per sync run.",
            default=200,
            section="general",
            surface="timeline",
            order=40,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.analysis_features",
            type="tags",
            label="Analysis Features",
            description="Metadata extraction capabilities to apply.",
            default=["exif"],
            options=[
                ExtensionFieldOption(label="EXIF Metadata", value="exif"),
                ExtensionFieldOption(label="GPS Geocoding", value="geocode"),
            ],
            section="general",
            surface="timeline",
            order=45,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.settle_window_hours",
            type="number",
            label="Session Settle Window (hours)",
            description=(
                "A photo session is emitted to the timeline only after no "
                "new photos arrive for this many hours. Lower values surface "
                "today's photos faster; higher values group long outings "
                "more reliably."
            ),
            default=4,
            section="general",
            surface="timeline",
            order=50,
        ),
    ]


class PhotoLibraryPlugin(Plugin):
    """Registers the photo library timeline source."""

    def get_tools(self) -> list[type[object]]:
        sensors_settings = self.settings.get("sensors", {})
        settings: dict[str, Any] = {}
        if isinstance(sensors_settings, dict):
            settings = dict(sensors_settings.get("photo_library", {}))
        return build_photo_library_tool_classes(settings)

    def get_sensors(self) -> list[tuple[str, object, SensorSpec]]:
        settings: dict[str, Any] = {}
        sensors_settings = self.settings.get("sensors", {})
        if isinstance(sensors_settings, dict):
            settings = dict(sensors_settings.get("photo_library", {}))

        # Support both legacy source_path (string) and new source_paths (list)
        source_paths: list[str] = []
        raw_paths = settings.get("source_paths")
        if isinstance(raw_paths, list):
            source_paths = [str(p) for p in raw_paths if p]
        elif not raw_paths:
            legacy = settings.get("source_path")
            if legacy:
                source_paths = [str(legacy)]

        # Resolve exclude patterns
        raw_excludes = settings.get("exclude_patterns")
        exclude_patterns: list[str] = []
        if isinstance(raw_excludes, list):
            exclude_patterns = [str(p) for p in raw_excludes if p]

        sensor = PhotoLibraryTimelineSensor(
            source_paths=source_paths,
            max_items_per_sync=int(settings.get("max_items_per_sync", DEFAULT_SETTINGS["max_items_per_sync"])),
            analysis_features=list(settings.get("analysis_features", DEFAULT_SETTINGS["analysis_features"])),
            exclude_patterns=exclude_patterns,
            settle_window_seconds=float(
                settings.get("settle_window_hours", DEFAULT_SETTINGS["settle_window_hours"])
            ) * 3600.0,
        )
        return [
            (
                "timeline.photo_library",
                sensor,
                SensorSpec(
                    sensor_id="timeline.photo_library",
                    display_name="Photo Library",
                    description="Scan a local photo directory, extract EXIF metadata, and ingest into the timeline.",
                    domain="timeline",
                    surface="timeline",
                    sync_mode=str(settings.get("sync_mode", DEFAULT_SETTINGS["sync_mode"])),
                    polling_mode=getattr(sensor, "polling_mode", "interval"),
                    fields=_fields("sensors.photo_library"),
                    metadata={
                        "source_type": "photo_library",
                        "default_settings": dict(DEFAULT_SETTINGS),
                    },
                ),
            )
        ]

    def build_recall_artifacts(
        self,
        *,
        source_type: str,
        events: list[dict[str, Any]],
        query: str,
        query_mode: str | None,
    ) -> dict[str, object] | None:
        """Project photo-session memories into generic answer-facing asset refs."""
        _ = query, query_mode
        if source_type != "photo_library" or not events:
            return None

        asset_refs: list[dict[str, Any]] = []
        for event in events:
            asset_refs.extend(_build_recall_asset_refs(event))

        if not asset_refs:
            return None
        return {"asset_refs": asset_refs}

    def build_temporal_summary_features(
        self,
        *,
        source_type: str,
        events: list[dict[str, Any]],
        summary_category: str,
        period_start: float,
        period_end: float,
    ) -> dict[str, object] | None:
        """Aggregate session events into period-level features."""
        _ = summary_category, period_start, period_end
        if source_type != "photo_library":
            return None
        if not events:
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
            timeline = metadata.get("timeline")
            if not isinstance(timeline, dict):
                continue
            provenance = timeline.get("provenance")
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
            {"device": dev, "session_count": cnt}
            for dev, cnt in device_counter.most_common(3)
        ]
        top_locations = [
            {"location": loc, "session_count": cnt}
            for loc, cnt in location_counter.most_common(5)
        ]

        summary_lines: list[str] = []
        summary_lines.append(
            f"{len(events)} photo sessions across {len(days_active)} days, "
            f"{photo_total} photos in total."
        )
        if top_devices:
            joined = " and ".join(d["device"] for d in top_devices[:2])
            summary_lines.append(f"Most active devices: {joined}.")
        if top_locations:
            joined_locs = ", ".join(loc["location"] for loc in top_locations[:3])
            summary_lines.append(f"Visited: {joined_locs}.")

        return {
            "feature_type": "photo_library",
            "session_count": len(events),
            "photo_total": photo_total,
            "active_days": len(days_active),
            "devices": top_devices,
            "locations": top_locations,
            "gps_session_count": gps_session_count,
            "summary_lines": summary_lines,
        }


def _build_recall_asset_refs(event: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = event.get("metadata_json") if isinstance(event.get("metadata_json"), dict) else {}
    timeline = metadata.get("timeline") if isinstance(metadata.get("timeline"), dict) else {}
    if str(timeline.get("source_type") or "").strip() != "photo_library":
        return []

    provenance = timeline.get("provenance") if isinstance(timeline.get("provenance"), dict) else {}
    representative_photos = metadata.get("representative_photos")
    if not isinstance(representative_photos, list):
        return []

    title = str(timeline.get("title") or "").strip() or None
    location_name = str(provenance.get("location_name") or "").strip() or None
    device_name = str(provenance.get("device_name") or "").strip() or None
    session_source_item_id = str(timeline.get("source_item_id") or event.get("source_item_id") or "").strip() or None
    event_id = str(event.get("event_id") or "").strip() or None
    occurred_at = event.get("timestamp") or event.get("created_at")

    asset_refs: list[dict[str, Any]] = []
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

        asset_ref = {
            "asset_ref_id": asset_ref_id,
            "kind": "image",
            "event_id": event_id,
            "source_type": "photo_library",
            "source_item_id": asset_ref_id,
            "display_name": title,
            "captured_at": item.get("capture_ts") or provenance.get("first_capture_ts") or occurred_at,
            "occurred_at": occurred_at,
            "resolver_tool": "photo_library_resolve_photo_refs",
            "attributes": attributes,
        }
        asset_refs.append(
            {
                key: value
                for key, value in asset_ref.items()
                if value not in (None, "", [], {})
            }
        )

    return asset_refs
