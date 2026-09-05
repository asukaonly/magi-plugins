"""Tests for the Steam user-content clear boundary."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
from typing import Any

from magi_plugin_sdk import UserContentClearContext, UserContentClearRequest
from magi_plugin_sdk.fs import UnsafeManagedPathError
from magi_plugin_sdk.sensors import SensorSyncContext
import pytest

PLUGINS_ROOT = str(Path(__file__).resolve().parents[2])
if PLUGINS_ROOT not in sys.path:
    sys.path.insert(0, PLUGINS_ROOT)


class _RuntimePaths:
    def __init__(self, root: Path) -> None:
        self.root = root

    def plugin_cache_dir(self, plugin_id: str) -> Path:
        return self.root / plugin_id


class _ReaderTrap:
    def read_snapshot(self, **kwargs: Any) -> Any:
        raise AssertionError(
            "clear_user_content must not read Steam or use network I/O"
        )


def _context(runtime_paths: _RuntimePaths) -> UserContentClearContext:
    return UserContentClearContext(
        request=UserContentClearRequest(clear_generation=7),
        runtime_paths=runtime_paths,
        plugin_id="steam-play-history",
        sensor_id="timeline.steam_play_history",
        plugin_settings={
            "sensors": {
                "steam_play_history": {
                    "account_id": "configured-account",
                    "steam_path": "/configured/steam",
                }
            }
        },
    )


def test_clear_removes_collected_state_but_preserves_settings(
    tmp_path: Path,
) -> None:
    sensor_module = importlib.import_module("steam_play_history.sensor")
    runtime_paths = _RuntimePaths(tmp_path)
    state_path = runtime_paths.plugin_cache_dir("steam_play_history") / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "accounts": {
                    "account-hash": {
                        "games": {
                            "1145360": {
                                "name": "Hades",
                                "playtime_forever_minutes": 720,
                            }
                        }
                    }
                },
                "current_sessions": {
                    "account-hash:1145360": {
                        "game_name": "Hades",
                        "duration_seconds": 600,
                    }
                },
                "completed": [{"game_name": "Hades", "duration_seconds": 600}],
                "pending_delivery": [{"game_name": "Hades"}],
            }
        ),
        encoding="utf-8",
    )
    sensor = sensor_module.SteamPlayHistoryTimelineSensor(
        reader=_ReaderTrap(),
        steam_path="/configured/steam",
        account_id="configured-account",
    )
    context = _context(runtime_paths)

    async def run_clear() -> None:
        await sensor.clear_user_content(context)
        await sensor.clear_user_content(context)

    asyncio.run(run_clear())

    assert not state_path.exists()
    assert context.plugin_settings["sensors"]["steam_play_history"]["account_id"] == (
        "configured-account"
    )
    assert sensor.steam_path == "/configured/steam"
    assert sensor.account_id == "configured-account"


def test_clear_waits_for_collection_and_collection_can_resume(tmp_path: Path) -> None:
    reader_module = importlib.import_module("steam_play_history.reader")
    sensor_module = importlib.import_module("steam_play_history.sensor")
    runtime_paths = _RuntimePaths(tmp_path)
    operation_order: list[str] = []

    class _Reader:
        def read_snapshot(self, **kwargs: Any) -> Any:
            return reader_module.SteamSnapshot(
                account=None,
                games=[],
                source="local_vdf",
            )

    class _BlockingStateStore:
        def __init__(self) -> None:
            self.apply_started = asyncio.Event()
            self.release_apply = asyncio.Event()

        async def apply_snapshot(self, **kwargs: Any) -> None:
            operation_order.append("apply-start")
            self.apply_started.set()
            await self.release_apply.wait()
            operation_order.append("apply-end")

        async def flush_completed(self, **kwargs: Any) -> list[dict[str, Any]]:
            operation_order.append("flush")
            return []

        async def clear_user_content(self, **kwargs: Any) -> None:
            operation_order.append("clear")

    async def run_scenario() -> None:
        state_store = _BlockingStateStore()
        sensor = sensor_module.SteamPlayHistoryTimelineSensor(
            reader=_Reader(),
            state_store=state_store,
        )
        sync_context = SensorSyncContext(
        connection_id="test-connection",
            source_type="steam_play_history",
            manual=True,
            last_cursor="preserved-cursor",
            last_success_at=1_750_000_000.0,
            limit=10,
            runtime_paths=runtime_paths,
            plugin_settings={"sensors": {"steam_play_history": {}}},
        )
        collect_task = asyncio.create_task(sensor.collect_items(sync_context))
        await state_store.apply_started.wait()
        clear_task = asyncio.create_task(
            sensor.clear_user_content(_context(runtime_paths))
        )
        await asyncio.sleep(0)

        assert clear_task.done() is False
        state_store.release_apply.set()
        await collect_task
        await clear_task
        assert operation_order == ["apply-start", "apply-end", "flush", "clear"]
        assert sync_context.last_cursor == "preserved-cursor"

        operation_order.clear()
        await sensor.collect_items(sync_context)
        assert operation_order == ["apply-start", "apply-end", "flush"]

    asyncio.run(run_scenario())


def test_clear_removes_state_symlink_without_touching_target(tmp_path: Path) -> None:
    sensor_module = importlib.import_module("steam_play_history.sensor")
    runtime_paths = _RuntimePaths(tmp_path / "runtime")
    state_path = runtime_paths.plugin_cache_dir("steam_play_history") / "state.json"
    state_path.parent.mkdir(parents=True)
    external_state = tmp_path / "external-state.json"
    external_state.write_text('{"private": true}', encoding="utf-8")
    original = external_state.read_bytes()
    state_path.symlink_to(external_state)

    asyncio.run(
        sensor_module.SteamPlayHistoryTimelineSensor().clear_user_content(
            _context(runtime_paths)
        )
    )

    assert external_state.read_bytes() == original
    assert not os.path.lexists(state_path)


def test_clear_removes_hardlink_without_touching_other_links(tmp_path: Path) -> None:
    sensor_module = importlib.import_module("steam_play_history.sensor")
    runtime_paths = _RuntimePaths(tmp_path / "runtime")
    state_path = runtime_paths.plugin_cache_dir("steam_play_history") / "state.json"
    state_path.parent.mkdir(parents=True)
    external_state = tmp_path / "external-state.json"
    external_state.write_text('{"private": true}', encoding="utf-8")
    original = external_state.read_bytes()
    os.link(external_state, state_path)

    asyncio.run(
        sensor_module.SteamPlayHistoryTimelineSensor().clear_user_content(
            _context(runtime_paths)
        )
    )

    assert external_state.read_bytes() == original
    assert not state_path.exists()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "mkfifo"),
    reason="POSIX FIFOs are unavailable",
)
def test_clear_removes_fifo_without_opening_it(tmp_path: Path) -> None:
    sensor_module = importlib.import_module("steam_play_history.sensor")
    runtime_paths = _RuntimePaths(tmp_path / "runtime")
    state_path = runtime_paths.plugin_cache_dir("steam_play_history") / "state.json"
    state_path.parent.mkdir(parents=True)
    os.mkfifo(state_path)

    asyncio.run(
        sensor_module.SteamPlayHistoryTimelineSensor().clear_user_content(
            _context(runtime_paths)
        )
    )

    assert not os.path.lexists(state_path)


def test_clear_rejects_linked_state_directory(tmp_path: Path) -> None:
    sensor_module = importlib.import_module("steam_play_history.sensor")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    external_cache = tmp_path / "external-cache"
    external_cache.mkdir()
    external_state = external_cache / "state.json"
    external_state.write_text('{"private": true}', encoding="utf-8")
    original = external_state.read_bytes()
    (runtime_root / "steam_play_history").symlink_to(
        external_cache,
        target_is_directory=True,
    )

    with pytest.raises(UnsafeManagedPathError):
        asyncio.run(
            sensor_module.SteamPlayHistoryTimelineSensor().clear_user_content(
                _context(_RuntimePaths(runtime_root))
            )
        )

    assert external_state.read_bytes() == original


def test_clear_missing_state_does_not_create_through_linked_ancestor(
    tmp_path: Path,
) -> None:
    sensor_module = importlib.import_module("steam_play_history.sensor")
    external_root = tmp_path / "external"
    external_root.mkdir()
    linked_root = tmp_path / "linked-runtime"
    linked_root.symlink_to(external_root, target_is_directory=True)

    asyncio.run(
        sensor_module.SteamPlayHistoryTimelineSensor().clear_user_content(
            _context(_RuntimePaths(linked_root))
        )
    )

    assert not (external_root / "steam_play_history").exists()
