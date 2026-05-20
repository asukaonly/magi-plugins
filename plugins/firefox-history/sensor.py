"""Timeline sensor for local Firefox history."""
from __future__ import annotations

import sys
from pathlib import Path

_CORE_PARENT = Path(__file__).resolve().parents[1]
if str(_CORE_PARENT) not in sys.path:
    sys.path.append(str(_CORE_PARENT))

from browser_history_core.sensor_base import BaseBrowserHistoryTimelineSensor

from .firefox_reader import FirefoxHistoryReader


class FirefoxHistoryTimelineSensor(BaseBrowserHistoryTimelineSensor):
    """Pull-sync sensor backed by the local Firefox places.sqlite database."""

    sensor_id = "timeline.firefox_history"
    display_name = "Firefox History"
    source_type = "firefox_history"
    browser_code = "firefox"
    browser_label = "Firefox"

    def __init__(
        self,
        *,
        retention_mode: str | None = None,
        source_path: str | None = None,
        profile: str = "",
        merge_window_minutes: int = 30,
        reader: FirefoxHistoryReader | None = None,
    ) -> None:
        super().__init__(
            reader=reader or FirefoxHistoryReader(),
            retention_mode=retention_mode,
            source_path=source_path,
            profile=profile,
            merge_window_minutes=merge_window_minutes,
        )
