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
    assert profile.allowed_assertion_families == ["interest_profile"]
    assert profile.allow_assertion is True
    assert profile.allowed_assertion_traits == ["interest.*"]
    rule = profile.derived_assertion_specs[0]
    assert rule.rule_id == "steam_play_history.viewed_interest"
    assert rule.trait_family == "interest_profile"
    assert rule.signal_preset == "sustained_engagement"
    assert rule.durable_permitted is True
    assert rule.durable_min_observations == 6
    assert rule.durable_min_distinct_days == 3
    assert rule.durable_min_span_days == 14
