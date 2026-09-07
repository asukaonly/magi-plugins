"""Edge history timeline plugin."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from magi_plugin_sdk import Plugin, SourceSpec

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

from .edge_reader import _default_edge_root
from .source import EdgeHistoryTimelineSource


class EdgeHistoryPlugin(Plugin):
    """Registers the Edge history timeline source."""

    def get_extraction_profiles(self) -> list[Any]:
        return build_extraction_profiles("edge_history")

    def get_sources(self) -> list[tuple[str, object, SourceSpec]]:
        settings = {}
        sources_settings = self.settings.get("sources", {})
        if isinstance(sources_settings, dict):
            settings = dict(sources_settings.get("edge_history", {}))
        source = EdgeHistoryTimelineSource(
            retention_mode=str(settings.get("default_retention_mode") or DEFAULT_SETTINGS["default_retention_mode"]),
            source_path=str(settings.get("source_path") or _default_edge_root()),
            profile=str(settings.get("profile") or DEFAULT_SETTINGS["profile"]),
            merge_window_minutes=int(
                settings.get("merge_window_minutes", DEFAULT_SETTINGS["merge_window_minutes"])
            ),
        )
        return [
            (
                "timeline.edge_history",
                source,
                SourceSpec(
                    source_id="timeline.edge_history",
                    display_name="Edge History",
                    description="Local Microsoft Edge browsing history ingested into the user timeline.",
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
                        "source_type": "edge_history",
                        "default_settings": {
                            field.key.rsplit(".", 1)[-1]: field.model_copy(deep=True).default
                            for field in self.manifest.settings_fields
                            if field.key.startswith("sources.") and field.type != "secret"
                        },
                        "activation_flow": self.manifest.activation_flow.model_dump() if self.manifest.activation_flow is not None else None,
                        **build_browser_capability_metadata(
                            entry_id="edge",
                            entry_display_name="Edge",
                            entry_description="Local Microsoft Edge browsing history.",
                            entry_order=40,
                        ),
                    },
                ),
            )
        ]

    def get_summary_profiles(self) -> list[Any]:
        return [build_summary_profile(source_type="edge_history", plugin_id="edge-history")]

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
        if source_type != "edge_history":
            return None
        return build_temporal_summary_features(
            source_type=source_type,
            feature_type="edge_history",
            events=events,
            budget=budget,
        )
