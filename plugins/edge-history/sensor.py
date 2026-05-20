"""Timeline sensor for local Edge history."""
from __future__ import annotations

import sys
from pathlib import Path

_CORE_PARENT = Path(__file__).resolve().parents[1]
if str(_CORE_PARENT) not in sys.path:
    sys.path.append(str(_CORE_PARENT))

from browser_history_core.sensor_base import BaseBrowserHistoryTimelineSensor

from .edge_reader import EdgeHistoryReader


class EdgeHistoryTimelineSensor(BaseBrowserHistoryTimelineSensor):
    """Pull-sync sensor backed by the local Edge history SQLite database."""

    sensor_id = "timeline.edge_history"
    display_name = "Edge History"
    source_type = "edge_history"
    browser_code = "edge"
    browser_label = "Edge"

    def __init__(
        self,
        *,
        retention_mode: str | None = None,
        source_path: str | None = None,
        profile: str = "Default",
        merge_window_minutes: int = 30,
        reader: EdgeHistoryReader | None = None,
    ) -> None:
        super().__init__(
            reader=reader or EdgeHistoryReader(),
            retention_mode=retention_mode,
            source_path=source_path,
            profile=profile,
            merge_window_minutes=merge_window_minutes,
        )
