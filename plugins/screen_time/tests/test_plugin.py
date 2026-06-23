from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_plugin_class():
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "screen_time_plugin_under_test"
    spec = importlib.util.spec_from_file_location(
        package_name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)

    plugin_spec = importlib.util.spec_from_file_location(
        f"{package_name}.plugin",
        plugin_dir / "plugin.py",
    )
    assert plugin_spec is not None and plugin_spec.loader is not None
    module = importlib.util.module_from_spec(plugin_spec)
    sys.modules[plugin_spec.name] = module
    plugin_spec.loader.exec_module(module)
    return module.ScreenTimePlugin


def test_screen_time_profile_is_graph_only() -> None:
    cls = _load_plugin_class()
    plugin = cls()
    profile = plugin.get_extraction_profiles()[0]

    assert profile.profile_id == "source.screen_time"
    assert profile.source_types == ["screen_time"]
    assert profile.allow_graph is True
    assert profile.allow_assertion is False
    assert profile.assertion_mode == "none"
    assert profile.allowed_assertion_families == []
    assert profile.derived_assertion_specs == []
    assert profile.allowed_entity_types == ["software", "media"]
    assert profile.allowed_predicates == ["USES", "VIEWED"]
