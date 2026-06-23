"""GitHub Activity plugin registration."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_plugin_module():
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "github_activity_under_test"
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


def test_plugin_exposes_timeline_sensor_with_github_connection_action() -> None:
    plugin_mod = _load_plugin_module()
    plugin = plugin_mod.GitHubActivityPlugin()
    plugin.settings = {
        "sensors": {
            "github_activity": {
                "enabled": True,
                "access_token": "token",
                "repositories": ["acme/app"],
                "sync_interval_minutes": 30,
            }
        }
    }

    sensors = plugin.get_sensors()
    actions = plugin.get_settings_actions()

    assert len(sensors) == 1
    _, sensor, spec = sensors[0]
    assert sensor.repositories == ["acme/app"]
    assert spec.metadata["source_type"] == "github_activity"
    assert spec.metadata["activation_flow"]["enabled_key"] == "sensors.github_activity.enabled"
    field_keys = [field.key for field in spec.fields]
    assert "sensors.github_activity.client_id" in field_keys
    assert "sensors.github_activity.repositories" in field_keys
    assert any(action.action_id == "connect_github" for action in actions)


def test_extraction_profile_keeps_github_activity_structured_and_project_focused() -> None:
    plugin_mod = _load_plugin_module()
    plugin = plugin_mod.GitHubActivityPlugin()

    profile = plugin.get_extraction_profiles()[0]

    assert profile.profile_id == "source.github_activity"
    assert profile.source_types == ["github_activity"]
    assert set(profile.structured_allowed_predicates) == {"WORKS_WITH", "COMMITTED", "USES", "REFERENCES"}
    assert not ({"WORKED_ON", "REVIEWED", "OPENED", "CHECKED"} & set(profile.structured_allowed_predicates))
    assert profile.allow_assertion is False


def test_github_activity_declares_sensor_ui_i18n_keys() -> None:
    plugin_dir = Path(__file__).resolve().parents[1]
    zh = json.loads((plugin_dir / "i18n" / "zh-CN.json").read_text())
    en = json.loads((plugin_dir / "i18n" / "en.json").read_text())

    for payload in (zh, en):
        root = payload["github_activity"]
        assert root["name"]
        assert root["description"]
        assert root["entries"]["github_activity"]["display_name"]
        assert root["entries"]["github_activity"]["description"]
        assert root["fields"]["repositories"]["label"]
        assert root["activation"]["title"]

    assert zh["github_activity"]["name"] == "GitHub 活动"
    assert zh["github_activity"]["fields"]["repositories"]["label"] == "仓库"
