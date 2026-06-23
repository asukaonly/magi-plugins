"""Photo library timeline plugin."""
from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from errno import EACCES, EPERM
from pathlib import Path
from typing import Any

from magi_plugin_sdk import (
    ActivationFlowSpec,
    ExtensionFieldOption,
    ExtensionFieldSpec,
    ExtractionProfileSpec,
    Plugin,
    PluginSettingsResourceSpec,
    SensorSpec,
    SettingsUIBlockSpec,
)
from .apple_photos_reader import DEFAULT_PHOTOS_LIBRARY_PATH
from .photo_tools import build_photo_library_tool_classes
from .sensor import PhotoLibraryTimelineSensor


DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "source_mode": "apple_photos",
    "photos_library_path": "",
    "sync_mode": "interval",
    "sync_interval_minutes": 60,
    "source_paths": [],
    "exclude_patterns": ["**/thumbnails", "**/.cache", "**/Thumbs.db", "**/@eaDir"],
    "max_items_per_sync": 200,
    "analysis_features": ["exif"],
    "settle_window_hours": 4,
}

CAPABILITY_ID = "photo_library"
CAPABILITY_DISPLAY_NAME = "Photo Library"
CAPABILITY_DESCRIPTION = "Manage Apple Photos and local photo folders as timeline sources."
APPLE_PHOTOS_SOURCE_TYPE = "photo_library_apple_photos"
DIRECTORY_SOURCE_TYPE = "photo_library_directory"
PHOTO_LIBRARY_SOURCE_TYPES = {APPLE_PHOTOS_SOURCE_TYPE, DIRECTORY_SOURCE_TYPE}
PHOTO_LIBRARY_L2_ENTITY_TYPES = ["hardware", "place"]
PHOTO_LIBRARY_L2_PREDICATES = ["OWNS", "VISITED"]

ENTRY_DEFINITIONS: dict[str, dict[str, Any]] = {
    APPLE_PHOTOS_SOURCE_TYPE: {
        "entry_id": "apple_photos",
        "display_name": "Apple Photos",
        "description": "Read the macOS Photos library directly.",
        "source_mode": "apple_photos",
        "order": 10,
    },
    DIRECTORY_SOURCE_TYPE: {
        "entry_id": "directory",
        "display_name": "Local Photos",
        "description": "Scan exported folders or local photo directories.",
        "source_mode": "directory",
        "order": 20,
    },
}


def _apple_photos_available() -> bool:
    return sys.platform == "darwin"


def _default_activation_source_mode() -> str:
    return "apple_photos" if _apple_photos_available() else "directory"


def _budget_int(budget: object | None, key: str, default: int) -> int:
    if budget is None:
        return int(default)
    if isinstance(budget, dict):
        raw = budget.get(key, default)
    else:
        raw = getattr(budget, key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


def _default_settings_for(source_type: str) -> dict[str, Any]:
    defaults = dict(DEFAULT_SETTINGS)
    defaults["source_mode"] = ENTRY_DEFINITIONS[source_type]["source_mode"]
    return defaults


def _fields(prefix: str, source_type: str) -> list[ExtensionFieldSpec]:
    source_mode = ENTRY_DEFINITIONS[source_type]["source_mode"]
    fields = [
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
    ]
    if source_mode == "apple_photos":
        fields.append(
            ExtensionFieldSpec(
                key=f"{prefix}.photos_library_path",
                type="path",
                label="Custom Apple Photos Library",
                description=(
                    "Optional .photoslibrary path. Leave empty to use the "
                    "current system Photos library."
                ),
                default="",
                required=False,
                section="general",
                surface="timeline",
                order=14,
                placeholder=DEFAULT_PHOTOS_LIBRARY_PATH,
            )
        )
    else:
        fields.extend(
            [
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
            ]
        )
    fields.extend(
        [
        ExtensionFieldSpec(
            key=f"{prefix}.sync_mode",
            type="select",
            label="Sync Mode",
            description="How photo library should be synchronized.",
            default="interval",
            required=True,
            options=[
                ExtensionFieldOption(label="Manual", value="manual"),
                ExtensionFieldOption(label="Automatic", value="interval"),
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
    )
    return fields


def _build_activation_flow(prefix: str, source_type: str) -> ActivationFlowSpec:
    source_mode = ENTRY_DEFINITIONS[source_type]["source_mode"]
    entry_label = str(ENTRY_DEFINITIONS[source_type]["display_name"])
    fields: list[ExtensionFieldSpec] = []
    if source_mode == "apple_photos":
        fields = []
    else:
        fields.append(
            ExtensionFieldSpec(
                key=f"{prefix}.source_paths",
                type="path",
                label="Photo Directories",
                description="Local folders containing photos to scan. Add one or more.",
                default=[],
                required=True,
                section="activation",
                surface="timeline",
                order=10,
                placeholder="/path/to/photos",
            )
        )
    return ActivationFlowSpec(
        title=f"Enable {entry_label}",
        description=(
            f"{entry_label} is sensitive local data. Magi reads photo metadata "
            "(time, place, camera) to build your timeline."
        ),
        confirm_label="Enable source",
        cancel_label="Not now",
        enabled_key=f"{prefix}.enabled",
        configured_key=f"{prefix}.initial_sync_configured",
        fields=fields,
    )


def _settings_ui_blocks(prefix: str, source_type: str) -> list[SettingsUIBlockSpec]:
    if source_type != APPLE_PHOTOS_SOURCE_TYPE:
        return []
    return [
        SettingsUIBlockSpec(
            block_id="apple_photos_permissions",
            type="resource_picker",
            title="Apple Photos Access",
            description="Apple Photos mode needs osxphotos and macOS permission to read the Photos library database.",
            resource_name="apple_photos_permissions",
            value_key="_readonly",
            presentation="permission_status",
        ),
    ]


def _capability_metadata(source_type: str) -> dict[str, Any]:
    entry = ENTRY_DEFINITIONS[source_type]
    metadata = {
        "capability_id": CAPABILITY_ID,
        "capability_display_name": CAPABILITY_DISPLAY_NAME,
        "capability_description": CAPABILITY_DESCRIPTION,
        "entry_id": entry["entry_id"],
        "entry_display_name": entry["display_name"],
        "entry_description": entry["description"],
        "entry_order": entry["order"],
    }
    if source_type == APPLE_PHOTOS_SOURCE_TYPE:
        metadata["available"] = _apple_photos_available()
        metadata["platforms"] = ["darwin"]
        if not metadata["available"]:
            metadata["unavailable_reason"] = "Apple Photos is only available on macOS."
    return metadata


class PhotoLibraryPlugin(Plugin):
    """Registers the photo library timeline source."""

    def get_extraction_profiles(self) -> list[ExtractionProfileSpec]:
        return [
            ExtractionProfileSpec(
                profile_id=f"source.{source_type}",
                source_types=[source_type],
                allowed_entity_types=PHOTO_LIBRARY_L2_ENTITY_TYPES,
                allowed_predicates=PHOTO_LIBRARY_L2_PREDICATES,
                structured_allowed_entity_types=PHOTO_LIBRARY_L2_ENTITY_TYPES,
                structured_allowed_predicates=PHOTO_LIBRARY_L2_PREDICATES,
                allowed_assertion_families=[],
                allow_graph=True,
                allow_assertion=False,
                assertion_mode="none",
                derived_assertion_specs=[],
                extraction_instructions=(
                    "These events are settled photo sessions. The source already provides "
                    "high-confidence structured hints for camera hardware and visited places.\n"
                    "Only keep graph facts that connect the user to owned hardware or visited "
                    "places. Do not infer preferences, identity, residence, routines, mood, "
                    "or long-term conclusions from a photo session."
                ),
            )
            for source_type in (APPLE_PHOTOS_SOURCE_TYPE, DIRECTORY_SOURCE_TYPE)
        ]

    def get_tools(self) -> list[type[object]]:
        sensors_settings = self.settings.get("sensors", {})
        settings: dict[str, Any] = {}
        if isinstance(sensors_settings, dict):
            apple_settings = dict(sensors_settings.get(APPLE_PHOTOS_SOURCE_TYPE, {}))
            directory_settings = dict(sensors_settings.get(DIRECTORY_SOURCE_TYPE, {}))
            if apple_settings.get("enabled"):
                settings = {**apple_settings, "source_mode": "apple_photos"}
            else:
                settings = {**directory_settings, "source_mode": "directory"}
        return build_photo_library_tool_classes(settings)

    def get_sensors(self) -> list[tuple[str, object, SensorSpec]]:
        sensors_settings = self.settings.get("sensors", {})
        sensors_payload = sensors_settings if isinstance(sensors_settings, dict) else {}
        registered: list[tuple[str, object, SensorSpec]] = []
        for source_type in (APPLE_PHOTOS_SOURCE_TYPE, DIRECTORY_SOURCE_TYPE):
            entry = ENTRY_DEFINITIONS[source_type]
            settings = dict(sensors_payload.get(source_type, {}))
            defaults = _default_settings_for(source_type)
            source_mode = str(entry["source_mode"])

            source_paths: list[str] = []
            raw_paths = settings.get("source_paths")
            if isinstance(raw_paths, list):
                source_paths = [str(p) for p in raw_paths if p]

            raw_excludes = settings.get("exclude_patterns")
            exclude_patterns = [str(p) for p in raw_excludes if p] if isinstance(raw_excludes, list) else []

            sensor_id = f"timeline.photo_library.{entry['entry_id']}"
            sensor = PhotoLibraryTimelineSensor(
                sensor_id=sensor_id,
                source_type=source_type,
                display_name=str(entry["display_name"]),
                source_paths=source_paths,
                source_mode=source_mode,
                photos_library_path=str(
                    settings.get("photos_library_path", defaults["photos_library_path"])
                ),
                max_items_per_sync=int(settings.get("max_items_per_sync", defaults["max_items_per_sync"])),
                analysis_features=list(settings.get("analysis_features", defaults["analysis_features"])),
                exclude_patterns=exclude_patterns,
                settle_window_seconds=float(settings.get("settle_window_hours", defaults["settle_window_hours"])) * 3600.0,
            )
            prefix = f"sensors.{source_type}"
            metadata = {
                "source_type": source_type,
                "default_settings": defaults,
                "activation_flow": _build_activation_flow(prefix, source_type).model_dump(),
                "settings_ui_blocks": [
                    block.model_dump() for block in _settings_ui_blocks(prefix, source_type)
                ],
                **_capability_metadata(source_type),
            }
            registered.append(
                (
                    sensor_id,
                    sensor,
                    SensorSpec(
                        sensor_id=sensor_id,
                        display_name=str(entry["display_name"]),
                        description=str(entry["description"]),
                        domain="timeline",
                        surface="timeline",
                        sync_mode=str(settings.get("sync_mode", defaults["sync_mode"])),
                        polling_mode=getattr(sensor, "polling_mode", "interval"),
                        fields=_fields(prefix, source_type),
                        metadata=metadata,
                    ),
                )
            )
        return registered

    def get_settings_resources(self) -> list[PluginSettingsResourceSpec]:
        return [
            PluginSettingsResourceSpec(
                resource_name="apple_photos_permissions",
                resource_type="channel_status",
                description="Live macOS dependency and permission status for Apple Photos mode.",
            ),
        ]

    def read_settings_resource(self, resource_name: str) -> Any:
        if resource_name != "apple_photos_permissions":
            raise KeyError(resource_name)
        settings = _photo_library_settings(self.settings)
        photos_library_path = str(
            settings.get("photos_library_path", DEFAULT_PHOTOS_LIBRARY_PATH)
            or DEFAULT_PHOTOS_LIBRARY_PATH
        )
        library_status = _photos_library_access_status(photos_library_path)
        dependency_status = (
            "granted" if importlib.util.find_spec("osxphotos") is not None else "denied"
        )
        return {
            "items": [
                {
                    "id": "osxphotos_dependency",
                    "label": "osxphotos dependency",
                    "label_i18n_key": "photo_library.permissions.osxphotos_dependency.label",
                    "description": "Required to read Apple Photos metadata without parsing Photos internals directly.",
                    "description_i18n_key": "photo_library.permissions.osxphotos_dependency.description",
                    "status": dependency_status,
                    "required": True,
                },
                {
                    "id": "photos_library_access",
                    "label": "Photos Library Access",
                    "label_i18n_key": "photo_library.permissions.photos_library_access.label",
                    "description": "Required to read the local Photos library database. Grant Photos or Full Disk Access to the app running Magi if this is denied.",
                    "description_i18n_key": "photo_library.permissions.photos_library_access.description",
                    "status": library_status,
                    "required": True,
                    "settings_url": (
                        "x-apple.systempreferences:com.apple.preference.security?Privacy_Photos"
                    ),
                },
            ],
        }

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
        if source_type not in PHOTO_LIBRARY_SOURCE_TYPES or not events:
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
        budget: object | None = None,
    ) -> dict[str, object] | None:
        """Aggregate session events into period-level features."""
        _ = summary_category, period_start, period_end
        if source_type not in PHOTO_LIBRARY_SOURCE_TYPES:
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
        covered_event_count = len(events)
        total_event_count = _budget_int(budget, "total_event_count", covered_event_count)
        omitted_event_count = max(0, total_event_count - covered_event_count)
        if omitted_event_count > 0:
            summary_lines.append(
                f"Photo feature coverage used {covered_event_count} representative sessions; {omitted_event_count} additional sessions were compacted."
            )

        return {
            "feature_type": "photo_library",
            "session_count": covered_event_count,
            "total_event_count": total_event_count,
            "covered_event_count": covered_event_count,
            "omitted_event_count": omitted_event_count,
            "coverage_ratio": (covered_event_count / total_event_count) if total_event_count else None,
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
    event_source_type = str(timeline.get("source_type") or "").strip()
    if event_source_type not in PHOTO_LIBRARY_SOURCE_TYPES:
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
            "source_type": event_source_type,
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


def _photo_library_settings(plugin_settings: dict[str, Any]) -> dict[str, Any]:
    sensors_settings = plugin_settings.get("sensors", {})
    if isinstance(sensors_settings, dict):
        settings = sensors_settings.get(APPLE_PHOTOS_SOURCE_TYPE, {})
        if isinstance(settings, dict):
            return dict(settings)
    return {}


def _photos_library_access_status(photos_library_path: str) -> str:
    if sys.platform != "darwin":
        return "unknown"
    library_path = Path(photos_library_path or DEFAULT_PHOTOS_LIBRARY_PATH).expanduser()
    try:
        if not library_path.exists():
            return "unknown"
    except OSError as exc:
        if exc.errno in {EACCES, EPERM}:
            return "denied"
        return "unknown"

    database_dir = library_path / "database"
    candidates = [
        database_dir / "Photos.sqlite",
        database_dir / "photos.db",
        database_dir / "photos.sqlite",
    ]
    try:
        existing_candidates = [path for path in candidates if path.exists()]
    except OSError as exc:
        if exc.errno in {EACCES, EPERM}:
            return "denied"
        return "unknown"

    for candidate in existing_candidates:
        try:
            with candidate.open("rb") as handle:
                handle.read(1)
            return "granted"
        except PermissionError:
            return "denied"
        except OSError as exc:
            if exc.errno in {EACCES, EPERM}:
                return "denied"
            continue
    return "unknown"
