from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from magi_plugin_sdk.tools import ToolExecutionContext

from photo_library_core.photo_tools import (
    build_apple_photo_tool_classes,
    build_local_photo_tool_classes,
)


class _AppleReader:
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


class _DirectoryReader:
    def scan_directory(
        self,
        source_path,
        *,
        limit,
        min_modified_at,
        exclude_patterns,
        analysis_features,
    ):
        from types import SimpleNamespace

        return SimpleNamespace(
            items=[
                {
                    "asset_local_id": "dir-1",
                    "path": f"{source_path}/IMG.jpg",
                    "filename": "IMG.jpg",
                    "modified_at": 1_710_000_000.0,
                }
            ]
        )


def _run(tool, ids):
    return asyncio.run(
        tool.execute(
            {"asset_ref_ids": ids},
            ToolExecutionContext(agent_id="test"),
        )
    )


def test_apple_and_local_resolvers_have_distinct_names() -> None:
    apple = build_apple_photo_tool_classes({})[0]()
    local = build_local_photo_tool_classes({"source_paths": ["/photos"]})[0]()

    assert apple.schema.name == "apple_photos_resolve_photo_refs"
    assert local.schema.name == "local_photos_resolve_photo_refs"


def test_apple_resolver_does_not_resolve_directory_refs() -> None:
    tool_class = build_apple_photo_tool_classes({})[0]
    tool_class._apple_reader_factory = _AppleReader
    result = _run(tool_class(), ["apple-photos:A", "dir-1"])

    assert result.success is True
    assert result.data["resolved_count"] == 1
    assert result.data["missing_asset_ref_ids"] == ["dir-1"]


def test_local_resolver_does_not_resolve_apple_refs() -> None:
    tool_class = build_local_photo_tool_classes({"source_paths": ["/photos"]})[0]
    tool_class._reader_factory = _DirectoryReader
    result = _run(tool_class(), ["dir-1", "apple-photos:A"])

    assert result.success is True
    assert result.data["resolved_count"] == 1
    assert result.data["missing_asset_ref_ids"] == ["apple-photos:A"]
