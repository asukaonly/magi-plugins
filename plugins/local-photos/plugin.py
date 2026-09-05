"""Local Photos timeline plugin."""
from __future__ import annotations

from typing import Any

from magi_plugin_sdk import ExtractionProfileSpec, Plugin, SourceSpec

from photo_library_core.photo_tools import (
    LOCAL_PHOTOS_RESOLVER_TOOL,
    build_local_photo_tool_classes,
)
from photo_library_core.plugin_support import (
    DIRECTORY_SOURCE_TYPE,
    build_extraction_profile,
    build_recall_artifacts,
    build_source_registration,
    build_temporal_summary_features,
)


class LocalPhotosPlugin(Plugin):
    """Registers local photo folders as an independently installed source."""

    def get_extraction_profiles(self) -> list[ExtractionProfileSpec]:
        return [build_extraction_profile(DIRECTORY_SOURCE_TYPE)]

    def get_tools(self) -> list[type[object]]:
        sources_settings = self.settings.get("sources", {})
        settings: dict[str, Any] = {}
        if isinstance(sources_settings, dict):
            raw_settings = sources_settings.get(DIRECTORY_SOURCE_TYPE, {})
            if isinstance(raw_settings, dict):
                settings = dict(raw_settings)
        return build_local_photo_tool_classes(settings, connection_id=self.connection.connection_id)

    def get_sources(self) -> list[tuple[str, object, SourceSpec]]:
        return [
            build_source_registration(
                self.settings,
                source_type=DIRECTORY_SOURCE_TYPE,
                entry_id="directory",
                display_name="Local Photos",
                description="Scan exported folders or local photo directories.",
                source_mode="directory",
                entry_order=20,
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
        _ = query, query_mode
        return build_recall_artifacts(
            source_type=source_type,
            events=events,
            expected_source_type=DIRECTORY_SOURCE_TYPE,
            resolver_tool=LOCAL_PHOTOS_RESOLVER_TOOL,
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
            expected_source_type=DIRECTORY_SOURCE_TYPE,
            budget=budget,
        )
