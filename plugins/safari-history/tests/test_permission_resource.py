from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_module() -> ModuleType:
    package_name = "safari_history_permission_under_test"
    spec = importlib.util.spec_from_file_location(
        package_name,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)

    module_name = f"{package_name}.plugin"
    pspec = importlib.util.spec_from_file_location(module_name, PLUGIN_ROOT / "plugin.py")
    assert pspec is not None and pspec.loader is not None
    module = importlib.util.module_from_spec(pspec)
    sys.modules[module_name] = module
    pspec.loader.exec_module(module)
    return module


def test_get_settings_resources_declares_safari_permissions() -> None:
    plugin_mod = _load_plugin_module()
    plugin = plugin_mod.SafariHistoryPlugin()

    resources = plugin.get_settings_resources()

    assert len(resources) == 1
    assert resources[0].resource_name == "permissions"
    assert resources[0].resource_type == "channel_status"


def test_read_settings_resource_reports_full_disk_access(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_mod = _load_plugin_module()
    plugin = plugin_mod.SafariHistoryPlugin()
    monkeypatch.setattr(plugin_mod, "_full_disk_access_status", lambda: "denied")

    payload = plugin.read_settings_resource("permissions")

    assert isinstance(payload, dict)
    items = payload["items"]
    assert len(items) == 1
    item = items[0]
    assert item["id"] == "full_disk_access"
    assert item["label"] == "Full Disk Access"
    assert item["label_i18n_key"] == "safari_history.permissions.full_disk_access.label"
    assert item["description_i18n_key"] == "safari_history.permissions.full_disk_access.description"
    assert item["status"] == "denied"
    assert item["required"] is True
    assert item["settings_url"] == (
        "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
    )


def test_full_disk_access_status_reads_history_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_mod = _load_plugin_module()
    safari_root = tmp_path / "Safari"
    safari_root.mkdir()
    (safari_root / "History.db").write_bytes(b"sqlite")
    monkeypatch.setattr(plugin_mod, "_default_safari_root", lambda: str(safari_root))

    assert plugin_mod._full_disk_access_status() == "granted"


def test_full_disk_access_status_reports_denied_on_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_mod = _load_plugin_module()
    safari_root = tmp_path / "Safari"
    safari_root.mkdir()
    (safari_root / "History.db").write_bytes(b"sqlite")
    monkeypatch.setattr(plugin_mod, "_default_safari_root", lambda: str(safari_root))

    real_open = plugin_mod.Path.open

    def blocked_open(self, *args, **kwargs):
        if self.name == "History.db":
            raise PermissionError(1, "Operation not permitted", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(plugin_mod.Path, "open", blocked_open)

    assert plugin_mod._full_disk_access_status() == "denied"


def test_full_disk_access_status_reports_unknown_when_history_db_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_mod = _load_plugin_module()
    safari_root = tmp_path / "Safari"
    safari_root.mkdir()
    monkeypatch.setattr(plugin_mod, "_default_safari_root", lambda: str(safari_root))

    assert plugin_mod._full_disk_access_status() == "unknown"


def test_read_settings_resource_unknown_resource_raises_key_error() -> None:
    plugin_mod = _load_plugin_module()
    plugin = plugin_mod.SafariHistoryPlugin()

    with pytest.raises(KeyError):
        plugin.read_settings_resource("missing")


def test_sensor_metadata_exposes_permissions_settings_block() -> None:
    plugin_mod = _load_plugin_module()
    plugin = plugin_mod.SafariHistoryPlugin()
    plugin.settings = {"sensors": {"safari_history": {"source_path": "/tmp/Safari"}}}

    _, _, spec = plugin.get_sensors()[0]
    blocks = spec.metadata.get("settings_ui_blocks")

    assert isinstance(blocks, list)
    assert len(blocks) == 1
    block = blocks[0]
    assert block["block_id"] == "macos_permissions"
    assert block["type"] == "resource_picker"
    assert block["presentation"] == "permission_status"
    assert block["resource_name"] == "permissions"
