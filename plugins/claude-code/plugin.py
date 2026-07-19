"""Claude Code timeline source plugin."""
from __future__ import annotations

from typing import Any

from magi_plugin_sdk import ExtractionProfileSpec, Plugin, SensorSpec

from agent_history_core.plugin_support import (
    build_extraction_profile,
    build_sensor_registration,
)

SOURCE_TYPE = "claude_code_agent_history"


class ClaudeCodePlugin(Plugin):
    """Registers local Claude Code transcripts as an independent source."""

    def get_extraction_profiles(self) -> list[ExtractionProfileSpec]:
        return [build_extraction_profile(SOURCE_TYPE)]

    def get_sensors(self) -> list[tuple[str, Any, SensorSpec]]:
        return [
            build_sensor_registration(
                self.settings,
                agent="claude_code",
                source_type=SOURCE_TYPE,
                display_name="Claude Code",
                description="Your own turns from local Claude Code transcripts.",
                default_source_paths=["~/.claude/projects"],
                entry_order=10,
            )
        ]
