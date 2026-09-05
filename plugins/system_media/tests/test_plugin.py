"""Tests for system media plugin registration."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest


PLUGINS_ROOT = str(Path(__file__).resolve().parents[2])
if PLUGINS_ROOT not in sys.path:
    sys.path.insert(0, PLUGINS_ROOT)

from system_media.plugin import SystemMediaPlugin


@pytest.mark.parametrize("runtime_platform", ["darwin", "win32"])
def test_system_media_registers_as_local_now_playing_entry(runtime_platform: str) -> None:
    plugin = SystemMediaPlugin()

    with patch.object(sys, "platform", runtime_platform):
        sources = plugin.get_sources()

    assert len(sources) == 1
    _, _, spec = sources[0]
    assert spec.display_name == "Local Now Playing"
    assert spec.metadata["source_type"] == "system_media"
    assert spec.metadata["capability_id"] == "listening_history"
    assert spec.metadata["entry_id"] == "local_now_playing"
    assert spec.metadata["entry_display_name"] == "Local Now Playing"
    assert spec.metadata["entry_order"] == 20


def test_system_media_does_not_register_on_linux() -> None:
    plugin = SystemMediaPlugin()
    with patch.object(sys, "platform", "linux"):
        assert plugin.get_sources() == []


def test_system_media_profile_declares_derived_music_rule() -> None:
    plugin = SystemMediaPlugin()
    profile = plugin.get_extraction_profiles()[0]

    assert profile.profile_id == "source.system_media"
    assert profile.source_types == ["system_media"]
    assert profile.allow_assertion is True
    assert profile.allowed_assertion_families == ["interest_profile"]
    assert profile.allowed_assertion_traits == ["interest.*"]
    assert profile.allow_assertion is True
    rule = profile.derived_assertion_specs[0]
    assert rule.rule_id == "system_media.listened_interest"
    assert rule.trait_family == "interest_profile"
    assert rule.signal_preset == "sustained_engagement"
    assert rule.durable_permitted is True
    assert rule.durable_min_observations == 8
    assert rule.durable_min_distinct_days == 4
    assert rule.durable_min_span_days == 21
