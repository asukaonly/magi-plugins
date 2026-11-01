"""Git Activity plugin registration."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_plugin_module():
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "git_activity_plugin_under_test"
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


def test_git_activity_profile_derives_recurring_projects() -> None:
    plugin_mod = _load_plugin_module()
    plugin = plugin_mod.GitActivityPlugin()

    profile = plugin.get_extraction_profiles()[0]

    assert profile.profile_id == "source.git_activity"
    assert profile.source_types == ["git_activity"]
    assert profile.allow_graph is True
    assert profile.allow_assertion is True
    assert profile.allow_assertion is True
    assert profile.allowed_assertion_families == ["project_profile"]
    assert profile.allowed_assertion_traits == ["project.*"]
    rule = profile.derived_assertion_specs[0]
    assert rule.rule_id == "git_activity.recurring_project"
    assert rule.trait_family == "project_profile"
    assert rule.signal_preset == "sustained_engagement"
    assert rule.durable_permitted is True
    assert rule.durable_min_observations == 6
    assert rule.durable_min_distinct_days == 3
    assert rule.durable_min_span_days == 14


def test_git_source_declares_versioned_observation_contract() -> None:
    from sdk_test_support import bind_test_plugin

    plugin = bind_test_plugin(_load_plugin_module().GitActivityPlugin())
    spec = plugin.get_sources()[0][2]
    capability = spec.metadata["remote_collection"]
    assert capability["capability_id"] == "git_activity.observe"
    assert capability["protocol"] == "source.change.v1"
    assert capability["schema_id"] == "git_activity.session"
    assert capability["schema_version"] == 1
    assert capability["supports_resources"] is False
    schema = capability["payload_schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["activity_type"] == {"const": "session"}
    assert "plugin_version" not in capability
