from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from typing import Any
import urllib.request
from unittest.mock import MagicMock

from magi_plugin_sdk import UserContentClearContext, UserContentClearRequest
from magi_plugin_sdk.fs import UnsafeManagedPathError
import pytest


PLUGINS_ROOT = str(Path(__file__).resolve().parents[2])
if PLUGINS_ROOT not in sys.path:
    sys.path.insert(0, PLUGINS_ROOT)

from photo_library_core.file_index import FileIndexCache  # noqa: E402
from photo_library_core.reader import PhotoLibraryReader  # noqa: E402
from photo_library_core.sensor import PhotoLibraryTimelineSensor  # noqa: E402


class _RuntimePaths:
    def __init__(self, root: Path) -> None:
        self._root = root
        self.requested_plugin_ids: list[str] = []

    def plugin_cache_dir(self, plugin_id: str) -> Path:
        self.requested_plugin_ids.append(plugin_id)
        return self._root / plugin_id


def _clear_context(runtime_paths: _RuntimePaths) -> UserContentClearContext:
    return UserContentClearContext(
        request=UserContentClearRequest(clear_generation=7),
        runtime_paths=runtime_paths,
        plugin_id="local-photos",
        sensor_id="timeline.photo_library.directory",
        plugin_settings={
            "sensors": {
                "photo_library_directory": {
                    "enabled": True,
                    "source_paths": ["/private/photos"],
                    "cursor": "source-cursor-42",
                    "watermark_ts": 1_775_000_123.5,
                },
                "photo_library_apple_photos": {
                    "enabled": True,
                    "photos_library_path": "/Pictures/System.photoslibrary",
                    "account_id": "apple-photos-local-account",
                    "permission": "granted",
                },
            }
        },
    )


def test_clear_deletes_photo_index_and_preserves_non_user_cache(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runtime_paths = _RuntimePaths(tmp_path / "cache")
    source_type = "photo_library_directory"
    cache_dir = runtime_paths.plugin_cache_dir(source_type)
    cache_dir.mkdir(parents=True)

    source_dir = tmp_path / "source-photos"
    source_dir.mkdir()
    source_photo = source_dir / "private-photo.jpg"
    source_photo.write_bytes(b"private photo bytes")

    settings_path = cache_dir / "settings.json"
    cursor_path = cache_dir / "source_cursor.json"
    watermark_path = cache_dir / "source_watermark.json"
    cities_path = cache_dir / "cities1000.txt"
    admin_codes_path = cache_dir / "admin1CodesASCII.txt"
    account_path = cache_dir / "apple_photos_account.json"
    permission_path = cache_dir / "apple_photos_permission.json"
    backup_path = cache_dir / "file_index.db.backup"
    preserved_files = {
        settings_path: b'{"source_paths":["/private/photos"]}',
        cursor_path: b'{"cursor":"source-cursor-42"}',
        watermark_path: b'{"watermark_ts":1775000123.5}',
        cities_path: b"geonames city data",
        admin_codes_path: b"geonames admin data",
        account_path: b'{"account":"keep"}',
        permission_path: b'{"permission":"granted"}',
        backup_path: b"unrelated backup",
    }
    for path, content in preserved_files.items():
        path.write_bytes(content)

    index = FileIndexCache(cache_dir)
    sensitive_path = str(source_photo.resolve())
    sensitive_exif = {
        "camera_model": "Private Camera",
        "latitude": 31.2304,
        "longitude": 121.4737,
        "gps_note": "Private Home",
    }
    index.put(
        sensitive_path,
        1_775_000_000.0,
        source_photo.stat().st_size,
        "private-hash",
        sensitive_exif,
        1_775_000_001.0,
    )
    assert index.get(
        sensitive_path,
        1_775_000_000.0,
        source_photo.stat().st_size,
    ) == sensitive_exif
    assert index.user_content_paths[0].exists()
    assert index.user_content_paths[1].exists()
    assert index.user_content_paths[2].exists()
    index.user_content_paths[3].write_bytes(b"rollback journal")

    reader = PhotoLibraryReader(file_index=index)
    sensor = PhotoLibraryTimelineSensor(
        source_type=source_type,
        source_paths=[str(source_dir)],
        reader=reader,
    )
    context = _clear_context(runtime_paths)
    network_call = MagicMock(side_effect=AssertionError("clear must not use network"))
    monkeypatch.setattr(urllib.request, "urlretrieve", network_call)
    monkeypatch.setattr(urllib.request, "urlopen", network_call)

    async def clear_twice() -> None:
        await sensor.clear_user_content(context)
        await sensor.clear_user_content(context)

    asyncio.run(clear_twice())

    assert index._conn is None
    assert reader._file_index is None
    assert all(not path.exists() for path in index.user_content_paths)
    assert cache_dir.is_dir()
    for path, content in preserved_files.items():
        assert path.read_bytes() == content
    assert source_photo.read_bytes() == b"private photo bytes"
    assert runtime_paths.requested_plugin_ids[-2:] == [source_type, source_type]
    assert context.plugin_settings["sensors"][source_type]["cursor"] == "source-cursor-42"
    assert (
        context.plugin_settings["sensors"][source_type]["watermark_ts"]
        == 1_775_000_123.5
    )
    assert (
        context.plugin_settings["sensors"]["photo_library_apple_photos"][
            "permission"
        ]
        == "granted"
    )
    assert json.loads(settings_path.read_text(encoding="utf-8"))["source_paths"] == [
        "/private/photos"
    ]
    network_call.assert_not_called()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "mkfifo"),
    reason="POSIX FIFOs are unavailable",
)
def test_clear_removes_link_like_index_entries_without_touching_targets(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    index = FileIndexCache(cache_dir)
    db_path, wal_path, shm_path, journal_path = index.user_content_paths
    external_db = tmp_path / "external-db"
    external_wal = tmp_path / "external-wal"
    external_db.write_bytes(b"external database")
    external_wal.write_bytes(b"external wal")
    db_path.symlink_to(external_db)
    os.link(external_wal, wal_path)
    os.mkfifo(shm_path)
    journal_path.write_bytes(b"managed journal")
    backup_path = cache_dir / "file_index.db.backup"
    backup_path.write_bytes(b"unrelated backup")

    index.clear_user_content()

    assert all(not os.path.lexists(path) for path in index.user_content_paths)
    assert external_db.read_bytes() == b"external database"
    assert external_wal.read_bytes() == b"external wal"
    assert backup_path.read_bytes() == b"unrelated backup"


def test_clear_rejects_linked_cache_directory_without_touching_external_files(
    tmp_path: Path,
) -> None:
    external_cache = tmp_path / "external-cache"
    external_cache.mkdir()
    external_db = external_cache / "file_index.db"
    external_db.write_bytes(b"external database")
    linked_cache = tmp_path / "linked-cache"
    linked_cache.symlink_to(external_cache, target_is_directory=True)
    index = FileIndexCache(linked_cache)

    with pytest.raises(UnsafeManagedPathError):
        index.clear_user_content()

    assert external_db.read_bytes() == b"external database"


def test_clear_missing_index_does_not_create_cache_directory(tmp_path: Path) -> None:
    cache_dir = tmp_path / "missing-cache"

    FileIndexCache(cache_dir).clear_user_content()

    assert not cache_dir.exists()


def test_clear_waits_for_scan_and_later_scan_rebuilds_index(tmp_path: Path) -> None:
    runtime_paths = _RuntimePaths(tmp_path / "cache")
    source_type = "photo_library_directory"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    scan_started = threading.Event()
    release_scan = threading.Event()

    class _BlockingReader:
        def __init__(self) -> None:
            self._file_index: FileIndexCache | None = None
            self.calls = 0

        def scan_directory(self, *_args: Any, **_kwargs: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                scan_started.set()
                assert release_scan.wait(timeout=2)
                cache_key = "old-photo"
            else:
                cache_key = "new-photo"
            assert self._file_index is not None
            self._file_index.put(
                cache_key,
                1.0,
                1,
                f"{cache_key}-hash",
                {"cache_key": cache_key},
                1.0,
            )
            return SimpleNamespace(
                items=[],
                total_scanned=0,
                errors=0,
                has_more=False,
            )

    reader = _BlockingReader()
    sensor = PhotoLibraryTimelineSensor(
        source_type=source_type,
        source_paths=[str(source_dir)],
        reader=reader,  # type: ignore[arg-type]
    )
    sync_context = SimpleNamespace(
        plugin_settings={"sensors": {source_type: {}}},
        runtime_paths=runtime_paths,
        last_cursor=None,
        last_success_at=None,
    )
    cache_dir = runtime_paths.plugin_cache_dir(source_type)
    index_paths = FileIndexCache(cache_dir).user_content_paths

    async def scenario() -> None:
        collect_task = asyncio.create_task(sensor.collect_items(sync_context))
        while not scan_started.is_set():
            await asyncio.sleep(0)
        clear_task = asyncio.create_task(
            sensor.clear_user_content(_clear_context(runtime_paths))
        )
        await asyncio.sleep(0)
        assert not clear_task.done()

        release_scan.set()
        await collect_task
        await clear_task
        assert all(not os.path.lexists(path) for path in index_paths)

        result = await sensor.collect_items(sync_context)
        assert result.items == []

    asyncio.run(scenario())

    assert reader.calls == 2
    assert reader._file_index is not None
    assert reader._file_index.get("new-photo", 1.0, 1) == {
        "cache_key": "new-photo"
    }
