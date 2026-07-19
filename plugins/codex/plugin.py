"""Codex timeline source plugin."""
from __future__ import annotations

from typing import Any

from magi_plugin_sdk import ExtractionProfileSpec, Plugin, SensorSpec

from agent_history_core.plugin_support import (
    build_extraction_profile,
    build_sensor_registration,
)

SOURCE_TYPE = "codex_agent_history"


class CodexPlugin(Plugin):
    """Registers local Codex transcripts as an independent source."""

    def get_extraction_profiles(self) -> list[ExtractionProfileSpec]:
        return [build_extraction_profile(SOURCE_TYPE)]

    def get_sensors(self) -> list[tuple[str, Any, SensorSpec]]:
        return [
            build_sensor_registration(
                self.settings,
                agent="codex",
                source_type=SOURCE_TYPE,
                display_name="Codex",
                description="Your own turns from local Codex prompts and sessions.",
                default_source_paths=["~/.codex"],
                entry_order=20,
            )
        ]
