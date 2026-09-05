from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_plugin_class():
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "chrome_history_plugin_under_test"
    package = ModuleType(package_name)
    package.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.plugin",
        plugin_dir / "plugin.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.ChromeHistoryPlugin


def test_chrome_history_profile_declares_derived_interest_rule() -> None:
    cls = _load_plugin_class()
    plugin = cls()
    profile = plugin.get_extraction_profiles()[0]

    assert profile.profile_id == "source.chrome_history"
    assert profile.allow_assertion is True
    assert profile.allowed_assertion_families == ["interest_profile"]
    assert profile.allowed_assertion_traits == ["interest.*"]
    assert profile.allow_assertion is True

    rules = profile.derived_assertion_specs
    assert len(rules) == 1
    rule = rules[0]
    assert rule.rule_id == "chrome_history.content_interest"
    assert rule.trait_family == "interest_profile"
    assert rule.signal_preset == "passive_exposure"
    assert rule.durable_permitted is False
    assert rule.min_observations == 3
    assert rule.min_distinct_days == 2


def test_chrome_history_activation_flow_declares_first_context_overrides() -> None:
    cls = _load_plugin_class()
    plugin = cls()
    _sensor_id, _sensor, spec = plugin.get_sensors()[0]
    flow = spec.metadata["activation_flow"]

    assert flow["first_context"]["settings_overrides"] == {
        "sensors.chrome_history.initial_sync_policy": "lookback_days",
        "sensors.chrome_history.initial_sync_lookback_days": 7,
    }
    assert flow["first_context"]["max_items_per_sync"] == 200
