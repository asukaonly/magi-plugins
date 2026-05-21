"""Tests for the plugin's permission settings resource shape."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _ensure_screenshot_timeline_package() -> ModuleType:
    """Register the plugin directory as the ``screenshot_timeline`` package.

    The plugin module uses relative imports (``from .permissions import ...``)
    so we need a real package on ``sys.modules`` before importing ``plugin``.
    """
    package_name = "screenshot_timeline"
    if package_name in sys.modules:
        return sys.modules[package_name]
    init_path = PLUGIN_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        package_name,
        init_path,
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return module


def _load_plugin_module() -> ModuleType:
    _ensure_screenshot_timeline_package()
    module_name = "screenshot_timeline.plugin"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_ROOT / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_get_settings_resources_declares_permissions() -> None:
    plugin_mod = _load_plugin_module()
    plugin = plugin_mod.ScreenshotTimelinePlugin()
    resources = plugin.get_settings_resources()
    assert len(resources) == 1
    spec = resources[0]
    assert spec.resource_name == "permissions"
    assert spec.resource_type == "channel_status"


def test_read_settings_resource_permissions_shape() -> None:
    plugin_mod = _load_plugin_module()
    plugin = plugin_mod.ScreenshotTimelinePlugin()
    payload = plugin.read_settings_resource("permissions")

    assert isinstance(payload, dict)
    assert "items" in payload
    items = payload["items"]
    assert isinstance(items, list)
    assert len(items) == 2

    by_id = {item["id"]: item for item in items}
    assert set(by_id.keys()) == {"screen_recording", "accessibility"}

    valid_statuses = {"granted", "denied", "not_determined", "unknown"}
    screen = by_id["screen_recording"]
    assert screen["label"] == "Screen Recording"
    assert screen["required"] is True
    assert screen["status"] in valid_statuses
    assert screen["label_i18n_key"].startswith("settings.plugins.screenshot_timeline.permissions.")
    assert screen["description"]

    accessibility = by_id["accessibility"]
    assert accessibility["label"] == "Accessibility"
    assert accessibility["required"] is False
    assert accessibility["status"] in valid_statuses
    assert accessibility["label_i18n_key"].startswith("settings.plugins.screenshot_timeline.permissions.")


def test_read_settings_resource_unknown_resource_raises_key_error() -> None:
    plugin_mod = _load_plugin_module()
    plugin = plugin_mod.ScreenshotTimelinePlugin()
    with pytest.raises(KeyError):
        plugin.read_settings_resource("nonexistent")


def test_sensor_metadata_exposes_settings_ui_blocks() -> None:
    plugin_mod = _load_plugin_module()
    plugin = plugin_mod.ScreenshotTimelinePlugin()
    sensors = plugin.get_sensors()
    assert sensors, "expected at least one sensor"
    _, _, spec = sensors[0]
    blocks = spec.metadata.get("settings_ui_blocks")
    assert isinstance(blocks, list)
    assert len(blocks) == 1
    block = blocks[0]
    assert block["block_id"] == "macos_permissions"
    assert block["type"] == "resource_picker"
    assert block["presentation"] == "permission_status"
    assert block["resource_name"] == "permissions"
