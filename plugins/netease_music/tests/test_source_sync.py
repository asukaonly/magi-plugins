from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_source_module() -> ModuleType:
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "netease_music_source_sync_under_test"
    if package_name not in sys.modules:
        package_spec = importlib.util.spec_from_file_location(
            package_name,
            plugin_dir / "__init__.py",
            submodule_search_locations=[str(plugin_dir)],
        )
        assert package_spec is not None and package_spec.loader is not None
        package = importlib.util.module_from_spec(package_spec)
        sys.modules[package_name] = package
        package_spec.loader.exec_module(package)

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.source",
        plugin_dir / "source.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _RuntimePaths:
    def plugin_cache_dir(self, plugin_id: str) -> Path:
        return Path("/tmp") / plugin_id


class _Reader:
    def read_play_records(self, *, limit: int, **kwargs):
        return [
            {
                "track_id": f"track-{idx}",
                "track_name": f"Track {idx}",
                "artist_name": "Artist",
                "album_name": "Album",
                "play_duration_sec": 60,
                "track_duration_ms": 180000,
                "update_time": 1_710_000_000 + idx,
                "source": "local",
                "is_liked": False,
                "track_alias": [],
            }
            for idx in range(1, limit + 1)
        ]

    def get_latest_update_time(self, **kwargs) -> int:
        return 1_710_000_999


class _PreparingReader(_Reader):
    def __init__(self) -> None:
        self.prepared_root: Path | None = None

    def prepare_temp_storage(self, temp_root: Path) -> None:
        self.prepared_root = temp_root


def test_netease_marks_has_more_when_limit_is_full() -> None:
    mod = _load_source_module()
    source = mod.NeteaseMusicTimelineSource(reader=_Reader())

    from magi_plugin_sdk.sources import SourceSyncContext

    result = asyncio.run(
        source.collect_items(
            SourceSyncContext(
        connection_id="test-connection",
                source_type="netease_music",
                manual=True,
                last_cursor="1710000000",
                last_success_at=None,
                limit=2,
                runtime_paths=_RuntimePaths(),
                plugin_settings={"sources": {"netease_music": {}}},
            )
        )
    )

    assert result.stats["has_more"] is True
    assert result.next_cursor == "1710000002"


def test_netease_initial_sync_advances_cursor_when_items_are_read() -> None:
    mod = _load_source_module()
    source = mod.NeteaseMusicTimelineSource(reader=_Reader())

    from magi_plugin_sdk.sources import SourceSyncContext

    result = asyncio.run(
        source.collect_items(
            SourceSyncContext(
        connection_id="test-connection",
                source_type="netease_music",
                manual=True,
                last_cursor=None,
                last_success_at=None,
                limit=2,
                runtime_paths=_RuntimePaths(),
                plugin_settings={"sources": {"netease_music": {}}},
            )
        )
    )

    assert result.next_cursor == "1710000002"


def test_netease_prepares_plugin_owned_temp_storage_before_collection() -> None:
    mod = _load_source_module()
    reader = _PreparingReader()
    source = mod.NeteaseMusicTimelineSource(reader=reader)

    from magi_plugin_sdk.sources import SourceSyncContext

    asyncio.run(
        source.collect_items(
            SourceSyncContext(
        connection_id="test-connection",
                source_type="netease_music",
                manual=True,
                last_cursor="1710000000",
                last_success_at=None,
                limit=1,
                runtime_paths=_RuntimePaths(),
                plugin_settings={"sources": {"netease_music": {}}},
            )
        )
    )

    assert reader.prepared_root == (
        Path("/tmp")
        / source.plugin_id
        / "temporary-database-copies"
        / "netease-music"
    )
