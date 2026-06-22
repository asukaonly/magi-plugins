"""photo-library must expose an activation_flow with a required source_paths path field."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_plugin_module() -> ModuleType:
    """Load ``plugin.py`` for the hyphenated ``photo-library`` dir.

    The plugin dir is hyphenated (cannot be imported as a package) and has no
    ``__init__.py``, yet ``plugin.py`` uses relative imports
    (``from .sensor import ...``). We synthesize a parent package whose
    ``__path__`` points at the plugin dir, register it in ``sys.modules``, then
    load ``plugin.py`` as a submodule so its relative imports resolve.
    """
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "photo_library_under_test"
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
    return module


def _photo_sensors(module: ModuleType | None = None) -> list[tuple[str, object, object]]:
    photo_plugin = module or _load_plugin_module()
    inst = photo_plugin.PhotoLibraryPlugin()
    sensors = inst.get_sensors()
    assert sensors, "expected at least one sensor"
    return sensors


def _photo_sensor_metadata(module: ModuleType | None = None) -> dict:
    _id, _obj, spec = _photo_sensors(module)[0]
    return spec.metadata


def _photo_specs_by_source_type(module: ModuleType | None = None) -> dict[str, object]:
    specs: dict[str, object] = {}
    for _id, _obj, spec in _photo_sensors(module):
        specs[spec.metadata["source_type"]] = spec
    return specs


def test_photo_library_declares_independent_apple_and_directory_entries() -> None:
    specs = _photo_specs_by_source_type()

    assert set(specs) == {"photo_library_apple_photos", "photo_library_directory"}

    apple = specs["photo_library_apple_photos"]
    directory = specs["photo_library_directory"]

    assert apple.metadata["capability_id"] == "photo_library"
    assert directory.metadata["capability_id"] == "photo_library"
    assert apple.metadata["entry_id"] == "apple_photos"
    assert directory.metadata["entry_id"] == "directory"
    assert apple.display_name == "Apple Photos"
    assert directory.display_name == "Local Photos"


def test_photo_library_apple_entry_declares_activation_flow() -> None:
    meta = _photo_specs_by_source_type()["photo_library_apple_photos"].metadata
    flow = meta.get("activation_flow")
    assert flow is not None, "photo-library must declare an activation_flow"
    assert flow["enabled_key"] == "sensors.photo_library_apple_photos.enabled"
    assert flow["configured_key"] == "sensors.photo_library_apple_photos.initial_sync_configured"
    apple_path = next(
        (f for f in flow["fields"] if f["key"] == "sensors.photo_library_apple_photos.photos_library_path"),
        None,
    )
    assert apple_path is not None, "activation_flow must include the Apple Photos library path"
    assert all("source_mode" not in f["key"] for f in flow["fields"])


def test_photo_library_directory_entry_declares_activation_flow() -> None:
    meta = _photo_specs_by_source_type()["photo_library_directory"].metadata
    flow = meta.get("activation_flow")
    assert flow is not None, "directory entry must declare an activation_flow"
    assert flow["enabled_key"] == "sensors.photo_library_directory.enabled"
    assert flow["configured_key"] == "sensors.photo_library_directory.initial_sync_configured"
    source_paths = next(
        (f for f in flow["fields"] if f["key"] == "sensors.photo_library_directory.source_paths"),
        None,
    )
    assert source_paths is not None, "activation_flow must include the source_paths field"
    assert source_paths["type"] == "path" and source_paths["required"] is True
    assert all("source_mode" not in f["key"] for f in flow["fields"])


def test_photo_library_declares_apple_photos_permission_block() -> None:
    meta = _photo_specs_by_source_type()["photo_library_apple_photos"].metadata
    blocks = meta.get("settings_ui_blocks")
    assert isinstance(blocks, list)
    block = next(
        (item for item in blocks if item["block_id"] == "apple_photos_permissions"),
        None,
    )
    assert block is not None
    assert block["resource_name"] == "apple_photos_permissions"
    assert block["presentation"] == "permission_status"


def test_photo_library_marks_apple_photos_unavailable_off_macos(monkeypatch) -> None:
    photo_plugin = _load_plugin_module()
    monkeypatch.setattr(photo_plugin.sys, "platform", "win32")

    apple = _photo_specs_by_source_type(photo_plugin)["photo_library_apple_photos"]

    assert apple.metadata["available"] is False
    assert apple.metadata["unavailable_reason"] == "Apple Photos is only available on macOS."


def test_photo_library_reads_apple_photos_permission_resource() -> None:
    photo_plugin = _load_plugin_module()
    inst = photo_plugin.PhotoLibraryPlugin()
    resources = inst.get_settings_resources()

    assert resources[0].resource_name == "apple_photos_permissions"
    assert resources[0].resource_type == "channel_status"

    payload = inst.read_settings_resource("apple_photos_permissions")
    item_ids = {item["id"] for item in payload["items"]}
    assert item_ids == {"osxphotos_dependency", "photos_library_access"}
    assert {item["status"] for item in payload["items"]} <= {"granted", "denied", "unknown"}
    for item in payload["items"]:
        assert item["label_i18n_key"].startswith("photo_library.permissions.")
        assert item["description_i18n_key"].startswith("photo_library.permissions.")
