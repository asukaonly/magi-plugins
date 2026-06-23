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


def test_system_media_profile_declares_derived_music_rule() -> None:
    plugin = SystemMediaPlugin()
    profile = plugin.get_extraction_profiles()[0]

    assert profile.profile_id == "source.system_media"
    assert profile.source_types == ["system_media"]
    assert profile.assertion_mode == "derived"
    assert profile.allowed_assertion_families == ["preference_profile"]
    assert profile.allowed_assertion_traits == ["music.*"]
    assert profile.allow_assertion is True
    assert profile.derived_assertion_specs == [
        {
            "rule_id": "system_media.listened_interest",
            "source_predicates": ["LISTENED"],
            "source_types": ["system_media"],
            "trait_family": "preference_profile",
            "trait_name_template": "music.{object_slug}",
            "min_observations": 3,
            "min_distinct_days": 2,
            "object_types": ["media"],
            "source_domains": ["external_activity"],
            "value_strategy": "canonical_name",
        }
    ]
