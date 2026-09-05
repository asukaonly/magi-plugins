from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

from magi_plugin_sdk import UserContentClearContext, UserContentClearRequest
from magi_plugin_sdk.fs import UnsafeManagedPathError
import pytest


PLUGINS_ROOT = str(Path(__file__).resolve().parents[2])
if PLUGINS_ROOT not in sys.path:
    sys.path.insert(0, PLUGINS_ROOT)

from screen_time import source as source_module  # noqa: E402
from screen_time.source import ScreenTimeTimelineSource  # noqa: E402


class _RuntimePaths:
    def __init__(self, root: Path) -> None:
        self._root = root

    def plugin_cache_dir(self, plugin_id: str) -> Path:
        return self._root / plugin_id


def _clear_context(runtime_paths: _RuntimePaths) -> UserContentClearContext:
    return UserContentClearContext(
        request=UserContentClearRequest(clear_generation=1),
        runtime_paths=runtime_paths,
        plugin_id="screen-time",
        source_id="timeline.screen_time",
        plugin_settings={
            "sources": {
                "screen_time": {
                    "enabled": True,
                    "sync_interval_minutes": 15,
                }
            }
        },
    )


def _write_state(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "last_activation": {
                    "session_id": "old-session",
                    "canonical_id": "private-app",
                    "app_name": "Private App",
                    "observed_at": "2026-07-31T12:10:00+00:00",
                },
                "open_buckets": {
                    "2026-07-31T12:00:00+00:00::private-app": {
                        "app_name": "Private App",
                        "duration_seconds": 600,
                    }
                },
                "pending_batch": [{"app_name": "Private App"}],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def test_clear_erases_plugin_state_and_is_local(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runtime_paths = _RuntimePaths(tmp_path)
    cache_dir = runtime_paths.plugin_cache_dir("screen_time")
    state_path = cache_dir / "state.json"
    settings_path = cache_dir / "settings.json"
    credentials_path = cache_dir / "credentials.json"
    _write_state(state_path)
    settings_path.write_text('{"enabled": true}', encoding="utf-8")
    credentials_path.write_text('{"token": "keep"}', encoding="utf-8")

    def fail_if_watcher_is_created(**_kwargs: Any) -> None:
        raise AssertionError("clear must not start foreground observation")

    monkeypatch.setattr(
        source_module, "ForegroundAppWatcher", fail_if_watcher_is_created
    )
    source = ScreenTimeTimelineSource()
    context = _clear_context(runtime_paths)

    async def run_clear_twice() -> None:
        await source.clear_user_content(context)
        await source.clear_user_content(context)

    asyncio.run(run_clear_twice())

    assert not state_path.exists()
    assert settings_path.read_text(encoding="utf-8") == '{"enabled": true}'
    assert credentials_path.read_text(encoding="utf-8") == '{"token": "keep"}'
    assert context.plugin_settings["sources"]["screen_time"]["enabled"] is True


def test_clear_waits_for_watcher_and_future_collect_restarts(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runtime_paths = _RuntimePaths(tmp_path)
    state_path = runtime_paths.plugin_cache_dir("screen_time") / "state.json"
    _write_state(state_path)
    source = ScreenTimeTimelineSource()
    restarted_watchers: list[Any] = []

    async def scenario() -> None:
        stop_started = asyncio.Event()
        allow_stop = asyncio.Event()

        class BlockingWatcher:
            is_supported = True
            is_running = True

            async def stop(self) -> None:
                stop_started.set()
                await allow_stop.wait()
                self.is_running = False

        class RestartedWatcher:
            def __init__(self, **_kwargs: Any) -> None:
                self.is_supported = True
                self.is_running = False
                restarted_watchers.append(self)

            def start(self) -> bool:
                self.is_running = True
                return True

            async def stop(self) -> None:
                self.is_running = False

        source._watcher = BlockingWatcher()  # type: ignore[assignment]
        monkeypatch.setattr(source_module, "ForegroundAppWatcher", RestartedWatcher)

        clear_task = asyncio.create_task(
            source.clear_user_content(_clear_context(runtime_paths))
        )
        await stop_started.wait()

        collect_task = asyncio.create_task(
            source.collect_items(SimpleNamespace(runtime_paths=runtime_paths))
        )
        await asyncio.sleep(0)
        assert not clear_task.done()
        assert not collect_task.done()
        assert json.loads(state_path.read_text(encoding="utf-8"))["last_activation"]

        allow_stop.set()
        await clear_task
        result = await collect_task

        assert [change.payload for change in result.changes] == []
        assert len(restarted_watchers) == 1
        assert restarted_watchers[0].is_running is True

    asyncio.run(scenario())

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_activation"] is None
    assert state["open_buckets"] == {}


def test_clear_removes_state_symlink_without_touching_its_target(
    tmp_path: Path,
) -> None:
    runtime_paths = _RuntimePaths(tmp_path / "runtime")
    state_path = runtime_paths.plugin_cache_dir("screen_time") / "state.json"
    state_path.parent.mkdir(parents=True)
    external_state = tmp_path / "external-state.json"
    _write_state(external_state)
    original = external_state.read_bytes()
    state_path.symlink_to(external_state)

    asyncio.run(
        ScreenTimeTimelineSource().clear_user_content(
            _clear_context(runtime_paths)
        )
    )

    assert external_state.read_bytes() == original
    assert not os.path.lexists(state_path)


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "mkfifo"),
    reason="POSIX FIFOs are unavailable",
)
def test_clear_removes_fifo_without_opening_it(tmp_path: Path) -> None:
    runtime_paths = _RuntimePaths(tmp_path / "runtime")
    state_path = runtime_paths.plugin_cache_dir("screen_time") / "state.json"
    state_path.parent.mkdir(parents=True)
    os.mkfifo(state_path)

    asyncio.run(
        ScreenTimeTimelineSource().clear_user_content(
            _clear_context(runtime_paths)
        )
    )

    assert not os.path.lexists(state_path)


def test_clear_removes_hardlink_without_touching_other_links(
    tmp_path: Path,
) -> None:
    runtime_paths = _RuntimePaths(tmp_path / "runtime")
    state_path = runtime_paths.plugin_cache_dir("screen_time") / "state.json"
    state_path.parent.mkdir(parents=True)
    external_state = tmp_path / "external-state.json"
    _write_state(external_state)
    original = external_state.read_bytes()
    os.link(external_state, state_path)

    asyncio.run(
        ScreenTimeTimelineSource().clear_user_content(
            _clear_context(runtime_paths)
        )
    )

    assert external_state.read_bytes() == original
    assert not state_path.exists()


def test_clear_missing_state_does_not_create_through_linked_ancestor(
    tmp_path: Path,
) -> None:
    external_root = tmp_path / "external"
    external_root.mkdir()
    linked_root = tmp_path / "linked-runtime"
    linked_root.symlink_to(external_root, target_is_directory=True)
    runtime_paths = _RuntimePaths(linked_root)

    asyncio.run(
        ScreenTimeTimelineSource().clear_user_content(
            _clear_context(runtime_paths)
        )
    )

    assert not (external_root / "screen_time").exists()


def test_clear_rejects_linked_cache_directory(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    external_cache = tmp_path / "external-cache"
    external_state = external_cache / "state.json"
    _write_state(external_state)
    original = external_state.read_bytes()
    (runtime_root / "screen_time").symlink_to(external_cache, target_is_directory=True)
    runtime_paths = _RuntimePaths(runtime_root)

    with pytest.raises(UnsafeManagedPathError):
        asyncio.run(
            ScreenTimeTimelineSource().clear_user_content(
                _clear_context(runtime_paths)
            )
        )

    assert external_state.read_bytes() == original
