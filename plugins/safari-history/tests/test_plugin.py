from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_plugin_class():
    plugin_dir = Path(__file__).resolve().parents[1]
    pkg = "safari_history_plugin_under_test"
    spec = importlib.util.spec_from_file_location(
        pkg, plugin_dir / "__init__.py", submodule_search_locations=[str(plugin_dir)]
    )
    assert spec is not None
    assert spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[pkg] = package
    spec.loader.exec_module(package)
    pspec = importlib.util.spec_from_file_location(f"{pkg}.plugin", plugin_dir / "plugin.py")
    assert pspec is not None
    assert pspec.loader is not None
    module = importlib.util.module_from_spec(pspec)
    sys.modules[pspec.name] = module
    pspec.loader.exec_module(module)
    return module.SafariHistoryPlugin


def test_plugin_registers_browser_core_sensor_spec() -> None:
    cls = _load_plugin_class()
    plugin = cls()
    plugin.settings = {
        "sensors": {
            "safari_history": {
                "enabled": False,
                "source_path": "/tmp/Safari",
                "sync_mode": "manual",
            }
        }
    }

    sensors = plugin.get_sensors()
    assert len(sensors) == 1
    sensor_id, sensor, spec = sensors[0]
    assert sensor_id == "timeline.safari_history"
    assert sensor.source_type == "safari_history"
    assert sensor.browser_code == "safari"
    assert spec.display_name == "Safari History"
    assert spec.sync_mode == "manual"
    assert spec.metadata["source_type"] == "safari_history"
    assert spec.metadata["activation_flow"]["enabled_key"] == "sensors.safari_history.enabled"


def test_plugin_declares_safari_extraction_and_summary_profiles() -> None:
    cls = _load_plugin_class()
    plugin = cls()
    plugin.settings = {}

    profile = plugin.get_extraction_profiles()[0]
    assert profile.profile_id == "source.safari_history"
    assert list(profile.source_types) == ["safari_history"]
    assert profile.assertion_mode == "derived"
    assert profile.allowed_assertion_families == ["preference_profile"]
    assert profile.allowed_assertion_traits == ["interest.*"]
    assert profile.allow_assertion is True
    assert profile.derived_assertion_specs == [
        {
            "rule_id": "safari_history.content_interest",
            "source_predicates": ["INTERESTED_IN"],
            "source_types": ["safari_history"],
            "trait_family": "preference_profile",
            "trait_name_template": "interest.{object_slug}",
            "min_observations": 3,
            "min_distinct_days": 2,
            "object_types": ["topic", "media", "person", "group", "organization", "product", "technology"],
            "source_domains": ["external_activity"],
            "value_strategy": "canonical_name",
        }
    ]

    summary = plugin.get_summary_profiles()[0]
    assert summary.profile_id == "safari-history:browser_activity"
    assert list(summary.source_types) == ["safari_history"]
