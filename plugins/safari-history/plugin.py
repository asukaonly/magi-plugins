"""Safari history timeline plugin."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from magi_plugin_sdk import Plugin, SensorSpec

_CORE_PARENT = Path(__file__).resolve().parents[1]
if str(_CORE_PARENT) not in sys.path:
    sys.path.append(str(_CORE_PARENT))

from browser_history_core.plugin_support import (
    DEFAULT_SETTINGS,
    build_activation_flow,
    build_extraction_profiles,
    build_fields,
    build_summary_profile,
    build_temporal_summary_features,
)

from .safari_reader import _default_safari_root
from .sensor import SafariHistoryTimelineSensor


class SafariHistoryPlugin(Plugin):
    """Registers the Safari history timeline source."""

    def get_extraction_profiles(self) -> list[Any]:
        return build_extraction_profiles("safari_history")

    def get_sensors(self) -> list[tuple[str, object, SensorSpec]]:
        settings = {}
        sensors_settings = self.settings.get("sensors", {})
        if isinstance(sensors_settings, dict):
            settings = dict(sensors_settings.get("safari_history", {}))
        sensor = SafariHistoryTimelineSensor(
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
                sensor,
                SensorSpec(
                    sensor_id="timeline.safari_history",
                    display_name="Safari History",
                    description="Local Safari browsing history ingested into the user timeline.",
                    domain="timeline",
                    surface="timeline",
                    sync_mode=str(settings.get("sync_mode", DEFAULT_SETTINGS["sync_mode"])),
                    polling_mode=getattr(sensor, "polling_mode", "interval"),
                    fields=build_fields(
                        "sensors.safari_history",
                        "Safari",
                        profile_default="",
                        profile_description="Safari stores history in one History.db file; leave this empty.",
                    ),
                    metadata={
                        "source_type": "safari_history",
                        "default_settings": {
                            **dict(DEFAULT_SETTINGS),
                            "profile": "",
                            "source_path": _default_safari_root(),
                        },
                        "activation_flow": build_activation_flow("sensors.safari_history", "Safari").model_dump(),
                    },
                ),
            )
        ]

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

    def build_condensed_summary_features(
        self,
        *,
        source_type: str,
        events: list[dict[str, Any]],
        budget: object | None = None,
    ) -> dict[str, object] | None:
        """Backwards-compatible alias for hosts that call condensed feature API."""

        if source_type != "safari_history":
            return None
        features = build_temporal_summary_features(
            source_type=source_type,
            feature_type="safari_history",
            events=events,
            budget=budget,
        )
        if not isinstance(features, dict):
            return None
        top_domains = features.get("top_domains")
        if isinstance(top_domains, list):
            features["top_entities"] = [
                {"type": "site", "domain": item.get("domain"), "count": item.get("count")}
                for item in top_domains
                if isinstance(item, dict)
            ]
        return features

    def build_compact_features(
        self,
        *,
        source_type: str,
        events: list[dict[str, Any]],
        budget: object | None = None,
    ) -> dict[str, object] | None:
        """Legacy alias expected by some runtimes."""

        return self.build_condensed_summary_features(
            source_type=source_type,
            events=events,
            budget=budget,
        )

