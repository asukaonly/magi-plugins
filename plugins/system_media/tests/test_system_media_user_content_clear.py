from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from magi_plugin_sdk import UserContentClearContext, UserContentClearRequest
from magi_plugin_sdk.fs import UnsafeManagedPathError
import pytest


PLUGINS_ROOT = str(Path(__file__).resolve().parents[2])
if PLUGINS_ROOT not in sys.path:
    sys.path.insert(0, PLUGINS_ROOT)

from system_media import sensor as sensor_module  # noqa: E402
from system_media.models import MediaState  # noqa: E402
from system_media.sensor import SystemMediaTimelineSensor  # noqa: E402


class _RuntimePaths:
    def __init__(self, root: Path) -> None:
        self._root = root

    def plugin_cache_dir(self, plugin_id: str) -> Path:
        return self._root / plugin_id


def _clear_context(runtime_paths: _RuntimePaths) -> UserContentClearContext:
    return UserContentClearContext(
        request=UserContentClearRequest(clear_generation=1),
        runtime_paths=runtime_paths,
        plugin_id="system-media",
        sensor_id="timeline.system_media",
        plugin_settings={
            "sensors": {
                "system_media": {
                    "enabled": True,
                    "min_session_seconds": 45,
                }
            }
        },
    )


def _write_state(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "current_session": {
                    "track_key": "music::private-track::private-artist",
                    "title": "Private Track",
                    "artist": "Private Artist",
                    "album": "Private Album",
                    "app_name": "Music",
                    "app_id": "music",
                    "started_at": "2026-07-31T12:00:00+00:00",
                    "last_seen_at": "2026-07-31T12:03:00+00:00",
                },
                "completed": [
                    {
                        "title": "Older Private Track",
                        "artist": "Private Artist",
                        "duration_seconds": 180,
                    }
                ],
                "pending_delivery": [{"title": "Private Track"}],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def test_clear_erases_sessions_and_does_not_poll(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runtime_paths = _RuntimePaths(tmp_path)
    cache_dir = runtime_paths.plugin_cache_dir("system_media")
    state_path = cache_dir / "state.json"
    settings_path = cache_dir / "settings.json"
    credentials_path = cache_dir / "credentials.json"
    _write_state(state_path)
    settings_path.write_text('{"min_session_seconds": 45}', encoding="utf-8")
    credentials_path.write_text('{"account": "keep"}', encoding="utf-8")

    media_reader = AsyncMock(side_effect=AssertionError("clear must not poll media"))
    monkeypatch.setattr(sensor_module, "get_current_media", media_reader)
    sensor = SystemMediaTimelineSensor()
    context = _clear_context(runtime_paths)

    async def run_clear_twice() -> None:
        await sensor.clear_user_content(context)
        await sensor.clear_user_content(context)

    asyncio.run(run_clear_twice())

    media_reader.assert_not_awaited()
    assert not state_path.exists()
    assert settings_path.read_text(encoding="utf-8") == '{"min_session_seconds": 45}'
    assert credentials_path.read_text(encoding="utf-8") == '{"account": "keep"}'
    assert context.plugin_settings["sensors"]["system_media"]["enabled"] is True


def test_clear_waits_for_active_collect_and_later_collect_recovers(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runtime_paths = _RuntimePaths(tmp_path)
    state_path = runtime_paths.plugin_cache_dir("system_media") / "state.json"
    sensor = SystemMediaTimelineSensor()

    async def scenario() -> None:
        poll_started = asyncio.Event()
        allow_poll = asyncio.Event()

        async def blocking_media_read() -> MediaState:
            poll_started.set()
            await allow_poll.wait()
            return MediaState(
                title="Old Track",
                artist="Old Artist",
                app_name="Music",
                app_id="music",
                playback_status="playing",
            )

        monkeypatch.setattr(sensor_module, "get_current_media", blocking_media_read)
        sync_context = SimpleNamespace(runtime_paths=runtime_paths)
        collect_task = asyncio.create_task(sensor.collect_items(sync_context))
        await poll_started.wait()

        clear_task = asyncio.create_task(
            sensor.clear_user_content(_clear_context(runtime_paths))
        )
        await asyncio.sleep(0)
        assert not clear_task.done()

        allow_poll.set()
        await collect_task
        await clear_task

        assert not state_path.exists()

        async def resumed_media_read() -> MediaState:
            return MediaState(
                title="New Track",
                artist="New Artist",
                app_name="Music",
                app_id="music",
                playback_status="playing",
            )

        monkeypatch.setattr(sensor_module, "get_current_media", resumed_media_read)
        result = await sensor.collect_items(sync_context)
        assert [change.payload for change in result.changes] == []

    asyncio.run(scenario())

    resumed_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert resumed_state["current_session"]["title"] == "New Track"
    assert resumed_state["current_session"]["artist"] == "New Artist"


def test_clear_removes_state_symlink_without_touching_target(tmp_path: Path) -> None:
    runtime_paths = _RuntimePaths(tmp_path / "runtime")
    state_path = runtime_paths.plugin_cache_dir("system_media") / "state.json"
    state_path.parent.mkdir(parents=True)
    external_state = tmp_path / "external-state.json"
    external_state.write_text('{"private": true}', encoding="utf-8")
    original = external_state.read_bytes()
    state_path.symlink_to(external_state)

    asyncio.run(
        SystemMediaTimelineSensor().clear_user_content(
            _clear_context(runtime_paths)
        )
    )

    assert external_state.read_bytes() == original
    assert not os.path.lexists(state_path)


def test_clear_removes_hardlink_without_touching_other_links(tmp_path: Path) -> None:
    runtime_paths = _RuntimePaths(tmp_path / "runtime")
    state_path = runtime_paths.plugin_cache_dir("system_media") / "state.json"
    state_path.parent.mkdir(parents=True)
    external_state = tmp_path / "external-state.json"
    external_state.write_text('{"private": true}', encoding="utf-8")
    original = external_state.read_bytes()
    os.link(external_state, state_path)

    asyncio.run(
        SystemMediaTimelineSensor().clear_user_content(
            _clear_context(runtime_paths)
        )
    )

    assert external_state.read_bytes() == original
    assert not state_path.exists()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "mkfifo"),
    reason="POSIX FIFOs are unavailable",
)
def test_clear_removes_fifo_without_opening_it(tmp_path: Path) -> None:
    runtime_paths = _RuntimePaths(tmp_path / "runtime")
    state_path = runtime_paths.plugin_cache_dir("system_media") / "state.json"
    state_path.parent.mkdir(parents=True)
    os.mkfifo(state_path)

    asyncio.run(
        SystemMediaTimelineSensor().clear_user_content(
            _clear_context(runtime_paths)
        )
    )

    assert not os.path.lexists(state_path)


def test_clear_rejects_linked_state_directory(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    external_cache = tmp_path / "external-cache"
    external_cache.mkdir()
    external_state = external_cache / "state.json"
    external_state.write_text('{"private": true}', encoding="utf-8")
    original = external_state.read_bytes()
    (runtime_root / "system_media").symlink_to(
        external_cache,
        target_is_directory=True,
    )

    with pytest.raises(UnsafeManagedPathError):
        asyncio.run(
            SystemMediaTimelineSensor().clear_user_content(
                _clear_context(_RuntimePaths(runtime_root))
            )
        )

    assert external_state.read_bytes() == original


def test_clear_missing_state_does_not_create_through_linked_ancestor(
    tmp_path: Path,
) -> None:
    external_root = tmp_path / "external"
    external_root.mkdir()
    linked_root = tmp_path / "linked-runtime"
    linked_root.symlink_to(external_root, target_is_directory=True)

    asyncio.run(
        SystemMediaTimelineSensor().clear_user_content(
            _clear_context(_RuntimePaths(linked_root))
        )
    )

    assert not (external_root / "system_media").exists()
