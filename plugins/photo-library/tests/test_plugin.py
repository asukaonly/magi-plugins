from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from magi_plugin_sdk.tools import ToolExecutionContext


def _load_plugin_class():
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "photo_library_plugin_under_test"
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
    return module.PhotoLibraryPlugin


def test_photo_library_declares_graph_only_l2_profiles() -> None:
    cls = _load_plugin_class()
    plugin = cls()
    profiles = plugin.get_extraction_profiles()

    by_source = {profile.source_types[0]: profile for profile in profiles}
    assert set(by_source) == {"photo_library_apple_photos", "photo_library_directory"}

    for source_type, profile in by_source.items():
        assert profile.profile_id == f"source.{source_type}"
        assert profile.allow_graph is True
        assert profile.allow_assertion is False
        assert profile.assertion_mode == "none"
        assert profile.allowed_assertion_families == []
        assert profile.allowed_entity_types == ["hardware", "place"]
        assert profile.allowed_predicates == ["OWNS", "VISITED"]
        assert profile.structured_allowed_entity_types == ["hardware", "place"]
        assert profile.structured_allowed_predicates == ["OWNS", "VISITED"]
        assert "preference_profile" not in profile.allowed_assertion_families


class _AppleReaderStub:
    def resolve_asset_refs(self, asset_ref_ids, photos_library_path):
        return (
            [
                {
                    "asset_local_id": asset_ref_id,
                    "path": f"/photos/{asset_ref_id}.HEIC",
                    "filename": f"{asset_ref_id}.HEIC",
                    "capture_timestamp": 1_710_000_000.0,
                }
                for asset_ref_id in asset_ref_ids
            ],
            [],
        )


def test_get_tools_resolves_apple_refs_without_enabled_toggle() -> None:
    """Sensor may ingest before enabled persists (backfill) or after disable;
    the resolver tool must still resolve apple-photos refs."""
    cls = _load_plugin_class()
    plugin = cls()
    plugin.settings = {
        "sensors": {
            "photo_library_apple_photos": {"enabled": False},
            "photo_library_directory": {},
        }
    }

    tool_class = plugin.get_tools()[0]
    tool_class._apple_reader_factory = _AppleReaderStub
    tool = tool_class()

    result = asyncio.run(
        tool.execute(
            {"asset_ref_ids": ["apple-photos:UUID-9"]},
            ToolExecutionContext(agent_id="test"),
        )
    )

    assert result.success is True
    assert result.data["file_paths"] == ["/photos/apple-photos:UUID-9.HEIC"]


def test_get_tools_passes_directory_settings_through() -> None:
    cls = _load_plugin_class()
    plugin = cls()
    plugin.settings = {
        "sensors": {
            "photo_library_apple_photos": {"enabled": True, "photos_library_path": "/Libs/A.photoslibrary"},
            "photo_library_directory": {"enabled": True, "source_paths": ["/exports"]},
        }
    }

    tool_class = plugin.get_tools()[0]

    assert tool_class._photos_library_path == "/Libs/A.photoslibrary"
    assert tool_class._source_paths == ["/exports"]
