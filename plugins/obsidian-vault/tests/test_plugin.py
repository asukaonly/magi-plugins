# tests/test_plugin.py
from __future__ import annotations
import importlib.util
import sys
from pathlib import Path


def _load_plugin_class():
    plugin_dir = Path(__file__).resolve().parents[1]
    pkg = "obsidian_vault_plugin_under_test"
    spec = importlib.util.spec_from_file_location(
        pkg, plugin_dir / "__init__.py", submodule_search_locations=[str(plugin_dir)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[pkg] = package
    spec.loader.exec_module(package)
    pspec = importlib.util.spec_from_file_location(f"{pkg}.plugin", plugin_dir / "plugin.py")
    module = importlib.util.module_from_spec(pspec)
    sys.modules[pspec.name] = module
    pspec.loader.exec_module(module)
    return module.ObsidianVaultPlugin


def _make_plugin(enabled: bool):
    cls = _load_plugin_class()
    plugin = cls()
    plugin.settings = {"sensors": {"obsidian_vault": {
        "enabled": enabled, "vault_path": "/tmp/vault",
        "exclude_folders": [".obsidian"], "cognition_exclude_folders": ["Clippings"],
    }}}
    return plugin


def test_get_sensors_returns_two_tiers_when_enabled() -> None:
    plugin = _make_plugin(enabled=True)
    sensors = plugin.get_sensors()
    ids = {sid for sid, _inst, _spec in sensors}
    assert ids == {"timeline.obsidian_vault.knowledge", "timeline.obsidian_vault.search"}
    cog = {sid: inst.memory_policy.cognition_eligible for sid, inst, _ in sensors}
    assert cog["timeline.obsidian_vault.knowledge"] is True
    assert cog["timeline.obsidian_vault.search"] is False


def test_get_sensors_empty_when_disabled() -> None:
    plugin = _make_plugin(enabled=False)
    assert plugin.get_sensors() == []


def test_extraction_profile_allows_reference_predicates() -> None:
    plugin = _make_plugin(enabled=True)
    profiles = plugin.get_extraction_profiles()
    assert len(profiles) == 1
    prof = profiles[0]
    assert "obsidian_vault" in prof.source_types
    assert "REFERENCES" in prof.allowed_predicates
    assert "TAGGED_AS" in prof.allowed_predicates
