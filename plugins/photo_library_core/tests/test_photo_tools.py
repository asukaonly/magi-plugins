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
    apple = build_apple_photo_tool_classes({}, connection_id="photos")[0]()
    local = build_local_photo_tool_classes({"source_paths": ["/photos"]}, connection_id="photos")[0]()

    assert apple.schema.name == "apple_photos_resolve_photo_refs"
    assert local.schema.name == "local_photos_resolve_photo_refs"


def test_apple_resolver_does_not_resolve_directory_refs() -> None:
    tool_class = build_apple_photo_tool_classes({}, connection_id="photos")[0]
    tool_class._apple_reader_factory = _AppleReader
    result = _run(tool_class(), ["apple-photos:A", "dir-1"])

    assert result.success is True
    assert result.data["resolved_count"] == 1
    assert result.data["asset_refs"][0]["connection_id"] == "photos"
    assert result.data["missing_asset_ref_ids"] == ["dir-1"]


def test_local_resolver_does_not_resolve_apple_refs() -> None:
    tool_class = build_local_photo_tool_classes({"source_paths": ["/photos"]}, connection_id="photos")[0]
    tool_class._reader_factory = _DirectoryReader
    result = _run(tool_class(), ["dir-1", "apple-photos:A"])

    assert result.success is True
    assert result.data["resolved_count"] == 1
    assert result.data["asset_refs"][0]["connection_id"] == "photos"
    assert result.data["missing_asset_ref_ids"] == ["apple-photos:A"]


def test_recall_refs_preserve_host_connection_and_native_session_id() -> None:
    from photo_library_core.plugin_support import build_recall_artifacts

    result = build_recall_artifacts(
        source_type="photo_library_directory", expected_source_type="photo_library_directory",
        resolver_tool="local_photos_resolve_photo_refs",
        events=[{
            "source_item_id": "source:host-namespaced-object", "timestamp": 1710000000.0,
            "metadata_json": {
                "source_connection_id": "photos-a", "source_object_id": "native-session",
                "activity_snapshot": {"source_type": "photo_library_directory", "source_item_id": "source:host-namespaced-object"},
                "representative_photos": [{"asset_local_id": "native-photo", "capture_ts": 1710000000.0}],
            },
        }],
    )
    ref = result["asset_refs"][0]
    assert ref["connection_id"] == "photos-a"
    assert ref["source_item_id"] == "native-photo"
    assert ref["attributes"]["session_source_item_id"] == "native-session"
