"""Codex timeline source plugin."""
from __future__ import annotations

from typing import Any

from magi_plugin_sdk import ExtractionProfileSpec, Plugin, SourceSpec

from agent_history_core.plugin_support import (
    build_extraction_profile,
    build_source_registration,
)

SOURCE_TYPE = "codex_agent_history"


class CodexPlugin(Plugin):
    """Registers local Codex transcripts as an independent source."""

    def get_extraction_profiles(self) -> list[ExtractionProfileSpec]:
        return [build_extraction_profile(SOURCE_TYPE)]

    def get_sources(self) -> list[tuple[str, Any, SourceSpec]]:
        return [
            build_source_registration(
                self.settings,
                manifest=self.manifest,
                agent="codex",
                source_type=SOURCE_TYPE,
                display_name="Codex",
                description="Your own turns from local Codex prompts and sessions.",
                default_source_paths=["~/.codex"],
                entry_order=20,
            )
        ]
