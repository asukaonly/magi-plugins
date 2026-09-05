from __future__ import annotations

import sys
from pathlib import Path


PLUGINS_ROOT = str(Path(__file__).resolve().parents[2])
if PLUGINS_ROOT not in sys.path:
    sys.path.insert(0, PLUGINS_ROOT)

from netease_music.plugin import NeteaseMusicPlugin


def test_netease_profile_declares_derived_music_rule() -> None:
    plugin = NeteaseMusicPlugin()
    profile = plugin.get_extraction_profiles()[0]

    assert profile.profile_id == "source.netease_music"
    assert profile.allow_assertion is True
    assert profile.allowed_assertion_families == ["interest_profile", "preference_profile"]
    assert profile.allowed_assertion_traits == ["interest.*", "preference.*"]
    assert profile.allow_assertion is True
    listened, liked = profile.derived_assertion_specs
    assert listened.rule_id == "netease_music.listened_interest"
    assert listened.trait_family == "interest_profile"
    assert listened.signal_preset == "sustained_engagement"
    assert listened.durable_permitted is True
    assert listened.durable_min_observations == 8
    assert listened.durable_min_distinct_days == 4
    assert listened.durable_min_span_days == 21
    assert liked.rule_id == "netease_music.explicit_like"
    assert liked.trait_family == "preference_profile"
    assert liked.signal_preset == "deliberate_choice"
    assert liked.durable_permitted is True
