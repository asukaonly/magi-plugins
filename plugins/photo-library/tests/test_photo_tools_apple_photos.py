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
