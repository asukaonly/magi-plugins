"""Terminal History plugin registration."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_plugin_module():
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "terminal_history_plugin_under_test"
    package_spec = importlib.util.spec_from_file_location(
        package_name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    assert package_spec is not None and package_spec.loader is not None
    package = importlib.util.module_from_spec(package_spec)
    sys.modules[package_name] = package
    package_spec.loader.exec_module(package)

    module_spec = importlib.util.spec_from_file_location(
        f"{package_name}.plugin",
        plugin_dir / "plugin.py",
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    return module


def test_terminal_history_profile_derives_recurring_tools() -> None:
    plugin_mod = _load_plugin_module()
    plugin = plugin_mod.TerminalHistoryPlugin()

    profile = plugin.get_extraction_profiles()[0]

    assert profile.profile_id == "source.terminal_history"
    assert profile.source_types == ["terminal_history"]
    assert profile.allow_graph is True
    assert profile.allow_assertion is True
    assert profile.assertion_mode == "derived"
    assert profile.allowed_assertion_families == ["routine_profile"]
    assert profile.allowed_assertion_traits == ["tool.*"]
    assert profile.derived_assertion_specs == [
        {
            "rule_id": "terminal_history.recurring_tool",
            "source_predicates": ["EXECUTED"],
            "source_types": ["terminal_history"],
            "trait_family": "routine_profile",
            "trait_name_template": "tool.{object_slug}",
            "min_observations": 3,
            "min_distinct_days": 2,
            "object_types": ["software"],
            "source_domains": ["external_activity"],
            "value_strategy": "canonical_name",
        }
    ]
