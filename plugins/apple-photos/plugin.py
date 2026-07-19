"""Apple Photos timeline plugin."""
from __future__ import annotations

import importlib.util
import sys
from errno import EACCES, EPERM
from pathlib import Path
from typing import Any

from magi_plugin_sdk import (
    ExtractionProfileSpec,
    Plugin,
    PluginSettingsResourceSpec,
    SensorSpec,
    SettingsUIBlockSpec,
)

from photo_library_core.apple_photos_reader import DEFAULT_PHOTOS_LIBRARY_PATH
from photo_library_core.photo_tools import (
    APPLE_PHOTOS_RESOLVER_TOOL,
    build_apple_photo_tool_classes,
)
from photo_library_core.plugin_support import (
    APPLE_PHOTOS_SOURCE_TYPE,
    build_extraction_profile,
    build_recall_artifacts,
    build_sensor_registration,
    build_temporal_summary_features,
)


class ApplePhotosPlugin(Plugin):
    """Registers Apple Photos as an independently installed source."""

    def get_extraction_profiles(self) -> list[ExtractionProfileSpec]:
        return [build_extraction_profile(APPLE_PHOTOS_SOURCE_TYPE)]

    def get_tools(self) -> list[type[object]]:
        sensors_settings = self.settings.get("sensors", {})
        settings: dict[str, Any] = {}
        if isinstance(sensors_settings, dict):
            raw_settings = sensors_settings.get(APPLE_PHOTOS_SOURCE_TYPE, {})
            if isinstance(raw_settings, dict):
                settings = dict(raw_settings)
        return build_apple_photo_tool_classes(settings)

    def get_sensors(self) -> list[tuple[str, object, SensorSpec]]:
        prefix = f"sensors.{APPLE_PHOTOS_SOURCE_TYPE}"
        permission_block = SettingsUIBlockSpec(
            block_id="apple_photos_permissions",
            type="resource_picker",
            title="Apple Photos Access",
            description=(
                "Apple Photos needs the local reader dependency and macOS "
                "permission to read the Photos library."
            ),
            resource_name="apple_photos_permissions",
            value_key="_readonly",
            presentation="permission_status",
        )
        return [
            build_sensor_registration(
                self.settings,
                source_type=APPLE_PHOTOS_SOURCE_TYPE,
                entry_id="apple_photos",
                display_name="Apple Photos",
                description="Read the macOS Photos library directly.",
                source_mode="apple_photos",
                entry_order=10,
                metadata_extra={
                    "available": sys.platform == "darwin",
                    "platforms": ["darwin"],
                    "unavailable_reason": (
                        None
                        if sys.platform == "darwin"
                        else "Apple Photos is only available on macOS."
                    ),
                    "settings_ui_blocks": [permission_block.model_dump()],
                    "settings_prefix": prefix,
                },
            )
        ]

    def get_settings_resources(self) -> list[PluginSettingsResourceSpec]:
        return [
            PluginSettingsResourceSpec(
                resource_name="apple_photos_permissions",
                resource_type="channel_status",
                description="Live dependency and permission status for Apple Photos.",
            )
        ]

    def read_settings_resource(self, resource_name: str) -> Any:
        if resource_name != "apple_photos_permissions":
            raise KeyError(resource_name)
        photos_library_path = _photos_library_path(self.settings)
        return {
            "items": [
                {
                    "id": "osxphotos_dependency",
                    "label": "Apple Photos reader",
                    "label_i18n_key": "apple_photos.permissions.osxphotos_dependency.label",
                    "description": "Required to read Apple Photos metadata.",
                    "description_i18n_key": (
                        "apple_photos.permissions.osxphotos_dependency.description"
                    ),
                    "status": (
                        "granted"
                        if importlib.util.find_spec("osxphotos") is not None
                        else "denied"
                    ),
                    "required": True,
                },
                {
                    "id": "photos_library_access",
                    "label": "Photos Library Access",
                    "label_i18n_key": "apple_photos.permissions.photos_library_access.label",
                    "description": "Required to read the local Photos library.",
                    "description_i18n_key": (
                        "apple_photos.permissions.photos_library_access.description"
                    ),
                    "status": _photos_library_access_status(photos_library_path),
                    "required": True,
                    "settings_url": (
                        "x-apple.systempreferences:com.apple.preference.security?Privacy_Photos"
                    ),
                },
            ]
        }

    def build_recall_artifacts(
        self,
        *,
        source_type: str,
        events: list[dict[str, Any]],
        query: str,
        query_mode: str | None,
    ) -> dict[str, object] | None:
        _ = query, query_mode
        return build_recall_artifacts(
            source_type=source_type,
            events=events,
            expected_source_type=APPLE_PHOTOS_SOURCE_TYPE,
            resolver_tool=APPLE_PHOTOS_RESOLVER_TOOL,
        )

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
        _ = summary_category, period_start, period_end
        return build_temporal_summary_features(
            source_type=source_type,
            events=events,
            expected_source_type=APPLE_PHOTOS_SOURCE_TYPE,
            budget=budget,
        )


def _photos_library_path(plugin_settings: dict[str, Any]) -> str:
    sensors_settings = plugin_settings.get("sensors", {})
    if isinstance(sensors_settings, dict):
        settings = sensors_settings.get(APPLE_PHOTOS_SOURCE_TYPE, {})
        if isinstance(settings, dict):
            return str(
                settings.get("photos_library_path", DEFAULT_PHOTOS_LIBRARY_PATH)
                or DEFAULT_PHOTOS_LIBRARY_PATH
            )
    return DEFAULT_PHOTOS_LIBRARY_PATH


def _photos_library_access_status(photos_library_path: str) -> str:
    if sys.platform != "darwin":
        return "unknown"
    library_path = Path(photos_library_path).expanduser()
    try:
        if not library_path.exists():
            return "unknown"
    except OSError as exc:
        if exc.errno in {EACCES, EPERM}:
            return "denied"
        return "unknown"
    candidates = [
        library_path / "database" / "Photos.sqlite",
        library_path / "database" / "photos.db",
        library_path / "database" / "photos.sqlite",
    ]
    for candidate in candidates:
        try:
            if not candidate.exists():
                continue
            with candidate.open("rb") as handle:
                handle.read(1)
            return "granted"
        except PermissionError:
            return "denied"
        except OSError as exc:
            if exc.errno in {EACCES, EPERM}:
                return "denied"
    return "unknown"
