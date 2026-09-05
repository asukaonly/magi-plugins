"""Tests for the NetEase Music user-content clear boundary."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from magi_plugin_sdk import UserContentClearContext, UserContentClearRequest

PLUGINS_ROOT = str(Path(__file__).resolve().parents[2])
if PLUGINS_ROOT not in sys.path:
    sys.path.insert(0, PLUGINS_ROOT)


class _RuntimePaths:
    def __init__(self, root: Path) -> None:
        self.root = root

    def plugin_cache_dir(self, plugin_id: str) -> Path:
        return self.root / plugin_id


def _context(runtime_paths: _RuntimePaths) -> UserContentClearContext:
    return UserContentClearContext(
        request=UserContentClearRequest(clear_generation=9),
        runtime_paths=runtime_paths,
        plugin_id="netease-music",
        source_id="timeline.netease_music",
        plugin_settings={
            "sources": {
                "netease_music": {
                    "db_path": "/configured/music.db",
                    "lastfm_api_key": "configured-secret",
                }
            }
        },
    )


class _SimulatedReparseDirectory:
    def __init__(self, path: str) -> None:
        self._path = path
        self.parent = Path("/")
        self.removed = False

    def __fspath__(self) -> str:
        return self._path

    def lstat(self) -> Any:
        return SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=getattr(
                stat,
                "FILE_ATTRIBUTE_REPARSE_POINT",
                0x0400,
            ),
        )

    def rmdir(self) -> None:
        self.removed = True

    def unlink(self) -> None:
        raise AssertionError("Directory reparse points must use rmdir")


def test_clear_removes_lastfm_cache_without_network_or_configuration_changes(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    source_module = importlib.import_module("netease_music.source")
    runtime_paths = _RuntimePaths(tmp_path)
    plugin_cache_dir = runtime_paths.plugin_cache_dir("netease-music")
    cursor_path = plugin_cache_dir / "cursor.txt"
    cursor_path.parent.mkdir(parents=True)
    cursor_path.write_text("preserved-cursor", encoding="utf-8")
    temporary_copy = (
        plugin_cache_dir
        / "temporary-database-copies"
        / "netease-music"
        / "copy-crashed"
    )
    temporary_copy.mkdir(parents=True)
    (temporary_copy / "database.db").write_bytes(b"private listening history")
    source_database = tmp_path / "configured-music.db"
    source_database.write_bytes(b"source must survive")
    external_file = tmp_path / "external.db"
    external_file.write_bytes(b"external must survive")
    (temporary_copy / "external-link").symlink_to(external_file)
    source = source_module.NeteaseMusicTimelineSource(
        source_path="/configured/music.db",
        tag_strategy="lastfm",
        lastfm_api_key="configured-secret",
    )
    source._lastfm_cache.update(
        {
            "artist|track": ["rock", "indie"],
            "artist|other": ["pop"],
        }
    )
    context = _context(runtime_paths)

    class _NetworkTrap:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"clear_user_content attempted network access: {name}")

    monkeypatch.setitem(sys.modules, "aiohttp", _NetworkTrap())

    async def run_clear() -> None:
        await source.clear_user_content(context)
        await source.clear_user_content(context)

    asyncio.run(run_clear())

    assert source._lastfm_cache == {}
    assert temporary_copy.exists() is False
    assert source_database.read_bytes() == b"source must survive"
    assert external_file.read_bytes() == b"external must survive"
    assert cursor_path.read_text(encoding="utf-8") == "preserved-cursor"
    assert source.source_path == "/configured/music.db"
    assert source.tag_strategy == "lastfm"
    assert source.lastfm_api_key == "configured-secret"
    assert context.plugin_settings["sources"]["netease_music"]["lastfm_api_key"] == (
        "configured-secret"
    )


def test_clear_waits_for_cache_writer_and_future_tag_fetches_resume(
    tmp_path: Path,
) -> None:
    source_module = importlib.import_module("netease_music.source")
    runtime_paths = _RuntimePaths(tmp_path)

    async def run_scenario() -> None:
        source = source_module.NeteaseMusicTimelineSource(
            tag_strategy="lastfm",
            lastfm_api_key="configured-secret",
        )
        fetch_started = asyncio.Event()
        release_fetch = asyncio.Event()

        async def blocked_fetch(artist: str, track: str) -> list[str]:
            fetch_started.set()
            await release_fetch.wait()
            source._lastfm_cache[f"{artist.lower()}|{track.lower()}"] = ["old-tag"]
            return ["old-tag"]

        source._fetch_lastfm_tags_locked = blocked_fetch
        fetch_task = asyncio.create_task(source._fetch_lastfm_tags("Artist", "Track"))
        await fetch_started.wait()
        clear_task = asyncio.create_task(
            source.clear_user_content(_context(runtime_paths))
        )
        await asyncio.sleep(0)

        assert clear_task.done() is False
        release_fetch.set()
        assert await fetch_task == ["old-tag"]
        await clear_task
        assert source._lastfm_cache == {}

        async def resumed_fetch(artist: str, track: str) -> list[str]:
            source._lastfm_cache[f"{artist.lower()}|{track.lower()}"] = ["new-tag"]
            return ["new-tag"]

        source._fetch_lastfm_tags_locked = resumed_fetch
        assert await source._fetch_lastfm_tags("Artist", "Track") == ["new-tag"]
        assert source._lastfm_cache == {"artist|track": ["new-tag"]}

    asyncio.run(run_scenario())


def test_windows_root_reparse_directory_is_removed_without_scanning(
    monkeypatch: Any,
) -> None:
    temp_storage = importlib.import_module("netease_music.temp_storage")
    reparse_directory = _SimulatedReparseDirectory("/simulated-root-junction")

    def fail_scandir(_path: Any) -> Any:
        raise AssertionError("Directory reparse points must never be scanned")

    monkeypatch.setattr(Path, "is_junction", lambda _path: False, raising=False)
    monkeypatch.setattr(temp_storage.os, "scandir", fail_scandir)

    temp_storage._clear_directory_contents_no_follow(reparse_directory)

    assert reparse_directory.removed is True


def test_windows_ancestor_reparse_directory_is_rejected_without_scanning(
    monkeypatch: Any,
) -> None:
    temp_storage = importlib.import_module("netease_music.temp_storage")
    reparse_directory = _SimulatedReparseDirectory(
        "/simulated-ancestor-junction"
    )

    def fail_scandir(_path: Any) -> Any:
        raise AssertionError("Directory reparse points must never be scanned")

    monkeypatch.setattr(Path, "is_junction", lambda _path: False, raising=False)
    monkeypatch.setattr(
        temp_storage,
        "_existing_components",
        lambda _path: [reparse_directory],
    )
    monkeypatch.setattr(temp_storage.os, "scandir", fail_scandir)

    with pytest.raises(
        temp_storage.UnsafeTemporaryStoragePathError,
        match="reparse point",
    ):
        temp_storage._assert_parent_chain_without_symlinks(
            Path("/managed/root")
        )

    assert reparse_directory.removed is False


def test_windows_child_reparse_directory_is_removed_without_scanning(
    monkeypatch: Any,
) -> None:
    temp_storage = importlib.import_module("netease_music.temp_storage")
    reparse_directory = _SimulatedReparseDirectory("/simulated-child-junction")

    def fail_scandir(_path: Any) -> Any:
        raise AssertionError("Directory reparse points must never be scanned")

    monkeypatch.setattr(Path, "is_junction", lambda _path: False, raising=False)
    monkeypatch.setattr(temp_storage.os, "scandir", fail_scandir)

    temp_storage._remove_entry_no_follow(reparse_directory)

    assert reparse_directory.removed is True
