"""Tests for system media plugin registration."""
from __future__ import annotations

import sys
from pathlib import Path


PLUGINS_ROOT = str(Path(__file__).resolve().parents[2])
if PLUGINS_ROOT not in sys.path:
    sys.path.insert(0, PLUGINS_ROOT)

from system_media.plugin import SystemMediaPlugin


def test_system_media_registers_as_local_now_playing_entry() -> None:
    plugin = SystemMediaPlugin()

    sensors = plugin.get_sensors()

    assert len(sensors) == 1
    _, _, spec = sensors[0]
    assert spec.display_name == "Local Now Playing"
    assert spec.metadata["source_type"] == "system_media"
    assert spec.metadata["capability_id"] == "listening_history"
    assert spec.metadata["entry_id"] == "local_now_playing"
    assert spec.metadata["entry_display_name"] == "Local Now Playing"
    assert spec.metadata["entry_order"] == 20
