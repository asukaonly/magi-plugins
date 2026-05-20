"""Screenshot Timeline plugin entry point."""
from __future__ import annotations

from typing import Any

from magi_plugin_sdk import Plugin, SensorSpec


class ScreenshotTimelinePlugin(Plugin):
    """Captures screen content with local OCR and feeds magi L1."""

    def sensors(self) -> list[SensorSpec]:
        # Will be filled in Task 10.
        return []

    def get_default_settings(self) -> dict[str, Any]:
        # Will be filled in Task 12.
        return {"enabled": False}
