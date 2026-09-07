# tests/test_plugin.py
from __future__ import annotations

from sdk_test_support import bind_test_plugin
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
    plugin = bind_test_plugin(cls())
    plugin.settings = {"sources": {"obsidian_vault": {
        "enabled": enabled, "vault_path": "/tmp/vault",
        "exclude_folders": [".obsidian"], "cognition_exclude_folders": ["Clippings"],
    }}}
    return plugin


def test_get_sources_returns_two_tiers_when_enabled() -> None:
    plugin = _make_plugin(enabled=True)
    sources = plugin.get_sources()
    ids = {sid for sid, _inst, _spec in sources}
    assert ids == {"timeline.obsidian_vault.knowledge", "timeline.obsidian_vault.search"}
    cog = {sid: inst.memory_policy.cognition_eligible for sid, inst, _ in sources}
    assert cog["timeline.obsidian_vault.knowledge"] is True
    assert cog["timeline.obsidian_vault.search"] is False
    for _, _, spec in sources:
        assert spec.metadata["capability_id"] == "obsidian_vault"
        assert spec.metadata["entry_id"] == "obsidian_vault"
        assert spec.metadata["entry_display_name"] == "Obsidian Vault"
    # Distinct source_type per tier so the host doesn't collide them: resolve/schedule/
    # cursor are keyed by (plugin_id, source_type), first-match-wins.
    src = {sid: inst.source_type for sid, inst, _ in sources}
    assert src["timeline.obsidian_vault.knowledge"] == "obsidian_vault"
    assert src["timeline.obsidian_vault.search"] == "obsidian_vault_search"
    spec_src = {sid: spec.metadata["source_type"] for sid, _inst, spec in sources}
    assert spec_src["timeline.obsidian_vault.search"] == "obsidian_vault_search"


def test_get_sources_stay_discoverable_when_disabled() -> None:
    plugin = _make_plugin(enabled=False)
    sources = plugin.get_sources()

    assert len(sources) == 2
    for _source_id, _source, spec in sources:
        flow = spec.metadata["activation_flow"]
        assert flow["enabled_key"] == "sources.obsidian_vault.enabled"
        assert flow["first_context"]["max_items_per_sync"] == 200
        vault_field = next(
            field
            for field in flow["fields"]
            if field["key"] == "sources.obsidian_vault.vault_path"
        )
        assert vault_field["path_kind"] == "directory"


def test_extraction_profile_uses_registry_predicates() -> None:
    plugin = _make_plugin(enabled=True)
    profiles = plugin.get_extraction_profiles()
    assert len(profiles) == 1
    prof = profiles[0]
    assert "obsidian_vault" in prof.source_types
    assert "REFERENCES" in prof.allowed_predicates
    assert list(prof.structured_allowed_predicates) == ["REFERENCES"]
    # Non-registry predicates/types would invalidate the WHOLE profile at host load.
    assert "TAGGED_AS" not in prof.allowed_predicates
    assert "MENTIONS" not in prof.allowed_predicates
    assert "note" not in prof.allowed_entity_types
