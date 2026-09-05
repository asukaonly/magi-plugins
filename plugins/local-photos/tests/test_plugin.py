from __future__ import annotations

import importlib.util
from sdk_test_support import bind_test_plugin

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


def _load_plugin_module() -> ModuleType:
    plugin_dir = Path(__file__).resolve().parents[1]
    plugins_dir = plugin_dir.parent
    if str(plugins_dir) not in sys.path:
        sys.path.insert(0, str(plugins_dir))
    package_name = "local_photos_plugin_under_test"
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


def test_local_photos_registers_only_the_directory_source() -> None:
    plugin = bind_test_plugin(_load_plugin_module().LocalPhotosPlugin())
    sensors = plugin.get_sensors()

    assert len(sensors) == 1
    _sensor_id, _sensor, spec = sensors[0]
    assert spec.metadata["source_type"] == "photo_library_directory"
    assert spec.metadata["entry_id"] == "directory"
    assert spec.metadata["capability_id"] == "photo_library"
    assert spec.display_name == "Local Photos"


def test_local_photos_activation_requires_a_folder() -> None:
    plugin = bind_test_plugin(_load_plugin_module().LocalPhotosPlugin())
    spec = plugin.get_sensors()[0][2]
    flow = spec.metadata["activation_flow"]

    assert flow["enabled_key"] == "sensors.photo_library_directory.enabled"
    source_paths = next(
        field
        for field in flow["fields"]
        if field["key"] == "sensors.photo_library_directory.source_paths"
    )
    assert source_paths["required"] is True


def test_local_photos_registers_only_its_extraction_profile() -> None:
    plugin = bind_test_plugin(_load_plugin_module().LocalPhotosPlugin())
    profiles = plugin.get_extraction_profiles()

    assert [profile.source_types for profile in profiles] == [["photo_library_directory"]]
    assert profiles[0].allowed_entity_types == ["hardware", "place"]
    assert profiles[0].allowed_predicates == ["OWNS", "VISITED"]
    assert profiles[0].allow_assertion is False


def test_local_photos_tool_uses_only_directory_settings() -> None:
    plugin = bind_test_plugin(_load_plugin_module().LocalPhotosPlugin())
    plugin.settings = {
        "sensors": {
            "photo_library_directory": {
                "source_paths": ["/exports"],
                "exclude_patterns": ["**/thumbs"],
            },
            "photo_library_apple_photos": {
                "photos_library_path": "/should/not/be/read.photoslibrary"
            },
        }
    }
    tool_class = plugin.get_tools()[0]

    assert tool_class._source_mode == "directory"
    assert tool_class._source_paths == ["/exports"]
    assert tool_class._photos_library_path == ""
    assert tool_class._tool_name == "local_photos_resolve_photo_refs"


def test_local_photos_manifest_has_no_apple_permission_or_dependency() -> None:
    import tomllib

    plugin_dir = Path(__file__).resolve().parents[1]
    manifest = tomllib.loads((plugin_dir / "plugin.toml").read_text())
    plugin = manifest["plugin"]
    capabilities = {
        item["capability"] for item in plugin["permissions"]["capabilities"]
    }

    assert plugin["entry_class"] == "LocalPhotosPlugin"
    assert plugin["depends_on"] == ["photo_library_core"]
    assert "dependencies" not in plugin
    assert capabilities == {"filesystem_read", "network"}
    assert not (plugin_dir / "requirements.lock").exists()


def test_local_resolver_does_not_claim_apple_refs() -> None:
    plugin = bind_test_plugin(_load_plugin_module().LocalPhotosPlugin())
    tool_class = plugin.get_tools()[0]
    tool_class._reader_factory = staticmethod(lambda: SimpleNamespace())
    tool = tool_class()

    assert tool.schema.name == "local_photos_resolve_photo_refs"
