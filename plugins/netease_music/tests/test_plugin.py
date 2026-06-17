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
    assert profile.assertion_mode == "derived"
    assert profile.allowed_assertion_families == ["preference_profile"]
    assert profile.allowed_assertion_traits == ["music.*"]
    assert profile.allow_assertion is True
    assert profile.derived_assertion_specs == [
        {
            "rule_id": "netease_music.listened_interest",
            "source_predicates": ["LISTENED"],
            "source_types": ["netease_music"],
            "trait_family": "preference_profile",
            "trait_name_template": "music.{object_slug}",
            "min_observations": 3,
            "min_distinct_days": 1,
            "source_domains": ["external_activity"],
            "value_strategy": "canonical_name",
        }
    ]
