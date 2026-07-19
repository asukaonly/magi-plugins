from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from magi_plugin_sdk.tools import ToolExecutionContext


def _load_tools_module() -> ModuleType:
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "photo_library_tools_apple_under_test"
    package = ModuleType(package_name)
    package.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.photo_tools",
        plugin_dir / "photo_tools.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _AppleReader:
    def resolve_asset_refs(self, asset_ref_ids: list[str], photos_library_path: str):
        assert photos_library_path == "/Photos Library.photoslibrary"
        assert asset_ref_ids == ["apple-photos:UUID-1"]
        return (
            [
                {
                    "asset_local_id": "apple-photos:UUID-1",
                    "apple_photos_uuid": "UUID-1",
                    "path": "/photos/IMG_0001.HEIC",
                    "filename": "IMG_0001.HEIC",
                    "capture_timestamp": 1_710_000_000.0,
                }
            ],
            [],
        )


class _AppleReaderAnyPath:
    """Apple reader fake that accepts any library path and echoes requested ids."""

    def resolve_asset_refs(self, asset_ref_ids: list[str], photos_library_path: str):
        items = [
            {
                "asset_local_id": asset_ref_id,
                "apple_photos_uuid": asset_ref_id.removeprefix("apple-photos:"),
                "path": f"/photos/{asset_ref_id.removeprefix('apple-photos:')}.HEIC",
                "filename": f"{asset_ref_id.removeprefix('apple-photos:')}.HEIC",
                "capture_timestamp": 1_710_000_000.0,
            }
            for asset_ref_id in asset_ref_ids
        ]
        return items, []


class _DirectoryReader:
    """Directory reader fake returning one item per scan rooted under the source path."""

    def scan_directory(self, source_path, *, limit, min_modified_at, exclude_patterns, analysis_features):
        from types import SimpleNamespace

        return SimpleNamespace(
            items=[
                {
                    "asset_local_id": "dir-asset-1",
                    "path": f"{source_path}/IMG_0002.jpg",
                    "filename": "IMG_0002.jpg",
                    "modified_at": 1_710_000_100.0,
                    "capture_timestamp": 1_710_000_100.0,
                }
            ]
        )


def _run(tool, asset_ref_ids):
    return asyncio.run(
        tool.execute(
            {"asset_ref_ids": asset_ref_ids},
            ToolExecutionContext(agent_id="test"),
        )
    )


def test_resolve_tool_uses_apple_reader_without_source_paths() -> None:
    mod = _load_tools_module()
    tool_class = mod.build_photo_library_tool_classes(
        {
            "source_mode": "apple_photos",
            "photos_library_path": "/Photos Library.photoslibrary",
            "source_paths": [],
        }
    )[0]
    tool_class._apple_reader_factory = _AppleReader
    tool = tool_class()

    result = asyncio.run(
        tool.execute(
            {"asset_ref_ids": ["apple-photos:UUID-1"]},
            ToolExecutionContext(agent_id="test"),
        )
    )

    assert result.success is True
    assert result.data["file_paths"] == ["/photos/IMG_0001.HEIC"]
    assert result.data["asset_refs"][0]["asset_ref_id"] == "apple-photos:UUID-1"


def test_resolve_tool_resolves_apple_refs_with_fresh_install_settings() -> None:
    """Fresh install: no enabled toggle, no source_paths — apple refs must still resolve."""
    mod = _load_tools_module()
    tool_class = mod.build_photo_library_tool_classes({})[0]
    tool_class._apple_reader_factory = _AppleReaderAnyPath
    tool = tool_class()

    result = _run(tool, ["apple-photos:UUID-1"])

    assert result.success is True
    assert result.data["file_paths"] == ["/photos/UUID-1.HEIC"]
    assert result.data["missing_asset_ref_ids"] == []


def test_resolve_tool_resolves_mixed_apple_and_directory_refs() -> None:
    mod = _load_tools_module()
    tool_class = mod.build_photo_library_tool_classes(
        {"source_paths": ["/photoslib"]}
    )[0]
    tool_class._apple_reader_factory = _AppleReaderAnyPath
    tool_class._reader_factory = _DirectoryReader
    tool = tool_class()

    result = _run(tool, ["apple-photos:UUID-1", "dir-asset-1"])

    assert result.success is True
    assert result.data["file_paths"] == ["/photos/UUID-1.HEIC", "/photoslib/IMG_0002.jpg"]
    assert result.data["missing_asset_ref_ids"] == []


def test_resolve_tool_keeps_hard_error_for_directory_only_without_source_paths() -> None:
    mod = _load_tools_module()
    tool_class = mod.build_photo_library_tool_classes({})[0]
    tool_class._apple_reader_factory = _AppleReaderAnyPath
    tool = tool_class()

    result = _run(tool, ["dir-asset-1"])

    assert result.success is False
    assert result.error == "photo_library source_paths are not configured."
    assert result.error_code == "INVALID_CONFIG"


def test_resolve_tool_marks_directory_refs_missing_when_unconfigured_but_apple_resolves() -> None:
    mod = _load_tools_module()
    tool_class = mod.build_photo_library_tool_classes({})[0]
    tool_class._apple_reader_factory = _AppleReaderAnyPath
    tool = tool_class()

    result = _run(tool, ["apple-photos:UUID-1", "dir-asset-1"])

    assert result.success is True
    assert result.data["file_paths"] == ["/photos/UUID-1.HEIC"]
    assert result.data["missing_asset_ref_ids"] == ["dir-asset-1"]
    assert "source_paths" in result.data["summary"]
