"""Safari history timeline plugin."""
from __future__ import annotations

import sys
from errno import EACCES, EPERM
from pathlib import Path
from typing import Any

from magi_plugin_sdk import Plugin, PluginSettingsResourceSpec, SourceSpec

_CORE_PARENT = Path(__file__).resolve().parents[1]
if str(_CORE_PARENT) not in sys.path:
    sys.path.append(str(_CORE_PARENT))

from browser_history_core.plugin_support import (
    DEFAULT_SETTINGS,
    build_browser_capability_metadata,
    build_extraction_profiles,
    build_summary_profile,
    build_temporal_summary_features,
)

from .safari_reader import _default_safari_root
from .source import SafariHistoryTimelineSource


def _full_disk_access_status() -> str:
    """Check whether this process can read Safari's protected History.db."""
    history_db = Path(_default_safari_root()).expanduser() / "History.db"
    try:
        with history_db.open("rb") as handle:
            handle.read(1)
    except FileNotFoundError:
        return "unknown"
    except PermissionError:
        return "denied"
    except OSError as exc:
        if exc.errno in {EACCES, EPERM}:
            return "denied"
        return "unknown"
    return "granted"


class SafariHistoryPlugin(Plugin):
    """Registers the Safari history timeline source."""

    def get_extraction_profiles(self) -> list[Any]:
        return build_extraction_profiles("safari_history")

    def get_sources(self) -> list[tuple[str, object, SourceSpec]]:
        settings = {}
        sources_settings = self.settings.get("sources", {})
        if isinstance(sources_settings, dict):
            settings = dict(sources_settings.get("safari_history", {}))
        source = SafariHistoryTimelineSource(
            retention_mode=str(settings.get("default_retention_mode") or DEFAULT_SETTINGS["default_retention_mode"]),
            source_path=str(settings.get("source_path") or _default_safari_root()),
            profile=str(settings.get("profile") or ""),
            merge_window_minutes=int(
                settings.get("merge_window_minutes", DEFAULT_SETTINGS["merge_window_minutes"])
            ),
        )
        return [
            (
                "timeline.safari_history",
                source,
                SourceSpec(
                    source_id="timeline.safari_history",
                    display_name="Safari History",
                    description="Local Safari browsing history ingested into the user timeline.",
                    domain="timeline",
                    surface="timeline",
                    sync_mode=str(settings.get("sync_mode", DEFAULT_SETTINGS["sync_mode"])),
                    polling_mode=getattr(source, "polling_mode", "interval"),
                    fields=[
                        field.model_copy(deep=True)
                        for field in sorted(self.manifest.settings_fields, key=lambda field: field.order)
                        if field.surface == "timeline" and field.section != "activation"
                    ],
                    metadata={
                        "source_type": "safari_history",
                        "default_settings": {
                            field.key.rsplit(".", 1)[-1]: field.model_copy(deep=True).default
                            for field in self.manifest.settings_fields
                            if field.key.startswith("sources.") and field.type != "secret"
                        },
                        "activation_flow": self.manifest.activation_flow.model_dump() if self.manifest.activation_flow is not None else None,
                        "settings_ui_blocks": [block.model_dump() for block in self.manifest.settings_ui_blocks],
                        **build_browser_capability_metadata(
                            entry_id="safari",
                            entry_display_name="Safari",
                            entry_description="Local Safari browsing history.",
                            entry_order=20,
                        ),
                    },
                ),
            )
        ]

    def get_settings_resources(self) -> list[PluginSettingsResourceSpec]:
        return [entry.model_copy(deep=True) for entry in self.manifest.settings_resources]

    def read_settings_resource(self, resource_name: str) -> Any:
        if resource_name != "permissions":
            raise KeyError(resource_name)
        return {
            "items": [
                {
                    "id": "full_disk_access",
                    "label": "Full Disk Access",
                    "label_i18n_key": "safari_history.permissions.full_disk_access.label",
                    "description": "Required to read the local Safari history database.",
                    "description_i18n_key": "safari_history.permissions.full_disk_access.description",
                    "status": _full_disk_access_status(),
                    "required": True,
                    "settings_url": (
                        "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
                    ),
                },
            ],
        }

    def get_summary_profiles(self) -> list[Any]:
        return [build_summary_profile(source_type="safari_history", plugin_id="safari-history")]

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
        if source_type != "safari_history":
            return None
        return build_temporal_summary_features(
            source_type=source_type,
            feature_type="safari_history",
            events=events,
            budget=budget,
        )
