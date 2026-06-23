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
    assert profile.assertion_mode == "derived"
    assert profile.allowed_assertion_families == ["preference_profile"]
    assert profile.allowed_assertion_traits == ["interest.*"]
    assert profile.allow_assertion is True

    rules = profile.derived_assertion_specs
    assert len(rules) == 1
    assert rules[0] == {
        "rule_id": "chrome_history.content_interest",
        "source_predicates": ["INTERESTED_IN"],
        "source_types": ["chrome_history"],
        "trait_family": "preference_profile",
        "trait_name_template": "interest.{object_slug}",
        "min_observations": 3,
        "min_distinct_days": 2,
        "object_types": ["topic", "media", "person", "group", "organization", "product", "technology"],
        "source_domains": ["external_activity"],
        "value_strategy": "canonical_name",
    }
