from __future__ import annotations

import importlib.util
from sdk_test_support import bind_test_plugin

import sys
from pathlib import Path
from types import ModuleType


def _load_plugin_module() -> ModuleType:
    plugin_dir = Path(__file__).resolve().parents[1]
    plugins_dir = plugin_dir.parent
    if str(plugins_dir) not in sys.path:
        sys.path.insert(0, str(plugins_dir))
    package_name = "apple_photos_plugin_under_test"
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


def test_apple_photos_registers_only_the_apple_source() -> None:
    plugin = bind_test_plugin(_load_plugin_module().ApplePhotosPlugin())
    sources = plugin.get_sources()

    assert len(sources) == 1
    _source_id, _source, spec = sources[0]
    assert spec.metadata["source_type"] == "photo_library_apple_photos"
    assert spec.metadata["entry_id"] == "apple_photos"
    assert spec.metadata["capability_id"] == "photo_library"
    assert spec.display_name == "Apple Photos"


def test_apple_photos_activation_does_not_request_a_folder() -> None:
    plugin = bind_test_plugin(_load_plugin_module().ApplePhotosPlugin())
    spec = plugin.get_sources()[0][2]
    flow = spec.metadata["activation_flow"]

    assert flow["enabled_key"] == "sources.photo_library_apple_photos.enabled"
    assert flow["fields"] == []
    assert any(
        field.key == "sources.photo_library_apple_photos.photos_library_path"
        for field in spec.fields
    )


def test_apple_photos_has_its_own_permission_status() -> None:
    plugin = bind_test_plugin(_load_plugin_module().ApplePhotosPlugin())
    spec = plugin.get_sources()[0][2]
    blocks = spec.metadata["settings_ui_blocks"]
    payload = plugin.read_settings_resource("apple_photos_permissions")

    assert blocks[0]["resource_name"] == "apple_photos_permissions"
    assert {item["id"] for item in payload["items"]} == {
        "osxphotos_dependency",
        "photos_library_access",
    }


def test_apple_photos_registers_only_its_extraction_profile() -> None:
    plugin = bind_test_plugin(_load_plugin_module().ApplePhotosPlugin())
    profiles = plugin.get_extraction_profiles()

    assert [profile.source_types for profile in profiles] == [
        ["photo_library_apple_photos"]
    ]
    assert profiles[0].allow_assertion is False


def test_apple_photos_tool_uses_only_apple_settings() -> None:
    plugin = bind_test_plugin(_load_plugin_module().ApplePhotosPlugin())
    plugin.settings = {
        "sources": {
            "photo_library_apple_photos": {
                "photos_library_path": "/Libs/A.photoslibrary"
            },
            "photo_library_directory": {"source_paths": ["/should/not/be/read"]},
        }
    }
    tool_class = plugin.get_tools()[0]

    assert tool_class._source_mode == "apple_photos"
    assert tool_class._photos_library_path == "/Libs/A.photoslibrary"
    assert tool_class._source_paths == []
    assert tool_class._tool_name == "apple_photos_resolve_photo_refs"


def test_apple_photos_manifest_has_only_apple_permissions() -> None:
    import tomllib

    plugin_dir = Path(__file__).resolve().parents[1]
    manifest = tomllib.loads((plugin_dir / "plugin.toml").read_text())
    plugin = manifest["plugin"]
    capabilities = {
        item["capability"] for item in plugin["permissions"]["capabilities"]
    }

    assert plugin["entry_class"] == "ApplePhotosPlugin"
    assert plugin["depends_on"] == ["photo_library_core"]
    assert plugin["platforms"] == ["macos"]
    assert capabilities == {"photos", "network"}
    assert "filesystem_read" not in capabilities
    assert (plugin_dir / "requirements.lock").exists()
