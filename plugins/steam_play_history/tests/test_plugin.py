"""Steam play history plugin registration."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_plugin_module():
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "steam_play_history_under_test"
    package_spec = importlib.util.spec_from_file_location(
        package_name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    package = importlib.util.module_from_spec(package_spec)
    sys.modules[package_name] = package
    package_spec.loader.exec_module(package)
    module_spec = importlib.util.spec_from_file_location(
        f"{package_name}.plugin",
        plugin_dir / "plugin.py",
    )
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    return module


def test_extraction_profile_derives_game_interest_from_repeated_play() -> None:
    plugin_mod = _load_plugin_module()
    plugin = plugin_mod.SteamPlayHistoryPlugin()

    profile = plugin.get_extraction_profiles()[0]

    assert profile.profile_id == "source.steam_play_history"
    assert profile.source_types == ["steam_play_history"]
    assert profile.allowed_entity_types == ["media", "software"]
    assert profile.structured_allowed_predicates == ["VIEWED", "INTERESTED_IN"]
    assert profile.allowed_assertion_families == ["preference_profile"]
    assert profile.assertion_mode == "derived"
    assert profile.allowed_assertion_traits == ["game.*"]
    assert profile.derived_assertion_specs == [
        {
            "rule_id": "steam_play_history.viewed_interest",
            "source_predicates": ["VIEWED"],
            "source_types": ["steam_play_history"],
            "trait_family": "preference_profile",
            "trait_name_template": "game.{object_slug}",
            "min_observations": 2,
            "min_distinct_days": 2,
            "object_types": ["media"],
            "source_domains": ["external_activity"],
            "value_strategy": "canonical_name",
        }
    ]
