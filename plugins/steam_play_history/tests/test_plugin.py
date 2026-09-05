"""Steam play history plugin registration."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from sdk_test_support import bind_test_plugin


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


@pytest.mark.parametrize("runtime_platform", ["darwin", "win32", "linux"])
@pytest.mark.parametrize("configured", [False, True])
def test_source_declaration_does_not_read_local_installation(
    runtime_platform: str, configured: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_mod = _load_plugin_module()
    reader_mod = sys.modules[f"{plugin_mod.__package__}.reader"]

    def reject_detection(*args, **kwargs):
        raise AssertionError("Source declarations must not inspect local Steam installations")

    monkeypatch.setattr(reader_mod, "_resolve_steam_root", reject_detection)
    configured_path = str(tmp_path / "Steam") if configured else ""
    plugin = bind_test_plugin(plugin_mod.SteamPlayHistoryPlugin(), settings={
        "sources": {"steam_play_history": {"steam_path": configured_path}},
    })
    with monkeypatch.context() as runtime:
        runtime.setattr(sys, "platform", runtime_platform)
        declarations = plugin.get_sources()

    assert len(declarations) == 1
    _, source, spec = declarations[0]
    assert source.steam_path == configured_path
    steam_path = next(field for field in spec.fields if field.key.endswith(".steam_path"))
    assert steam_path.default == ""
    assert spec.metadata["default_settings"]["steam_path"] == ""


@pytest.mark.parametrize("configured", [False, True])
def test_reader_resolves_installation_when_collecting(
    configured: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_mod = _load_plugin_module()
    reader_mod = sys.modules[f"{plugin_mod.__package__}.reader"]
    detected_root = tmp_path / "detected"
    configured_root = tmp_path / "configured"
    for root in (detected_root, configured_root):
        (root / "steamapps").mkdir(parents=True)
    monkeypatch.setattr(reader_mod, "_candidate_steam_roots", lambda: [detected_root])

    snapshot = reader_mod.SteamReader().read_snapshot(
        steam_path=str(configured_root) if configured else "",
    )

    assert snapshot.steam_path == str(configured_root if configured else detected_root)
    assert snapshot.errors == []
