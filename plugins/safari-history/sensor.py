"""Timeline sensor for local Safari history."""
from __future__ import annotations

import sys
from pathlib import Path

_CORE_PARENT = Path(__file__).resolve().parents[1]
if str(_CORE_PARENT) not in sys.path:
    sys.path.append(str(_CORE_PARENT))

from browser_history_core.sensor_base import BaseBrowserHistoryTimelineSensor

from .safari_reader import SafariHistoryReader


class SafariHistoryTimelineSensor(BaseBrowserHistoryTimelineSensor):
    """Pull-sync sensor backed by the local Safari history SQLite database."""

    sensor_id = "timeline.safari_history"
    display_name = "Safari History"
    source_type = "safari_history"
    browser_code = "safari"
    browser_label = "Safari"

    def __init__(
        self,
        *,
        retention_mode: str | None = None,
        source_path: str | None = None,
        profile: str = "",
        merge_window_minutes: int = 30,
        reader: SafariHistoryReader | None = None,
    ) -> None:
        super().__init__(
            reader=reader or SafariHistoryReader(),
            retention_mode=retention_mode,
            source_path=source_path,
            profile=profile,
            merge_window_minutes=merge_window_minutes,
        )

