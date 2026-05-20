"""Screenshot Timeline plugin entry point."""
from __future__ import annotations

from typing import Any

from magi_plugin_sdk import Plugin, SensorSpec

# Module-level defaults are the established sibling-plugin pattern
# (see plugins/calendar_plugin/plugin.py). Task 12 fills this in.
DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
}


class ScreenshotTimelinePlugin(Plugin):
    """Captures screen content with local OCR and feeds magi L1."""

    def get_sensors(self) -> list[tuple[str, Any, SensorSpec]]:
        # Returns (sensor_id, sensor_instance, sensor_spec) tuples.
        # Will be filled in Task 10 + Task 12.
        return []
