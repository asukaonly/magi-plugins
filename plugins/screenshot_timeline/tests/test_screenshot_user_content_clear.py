"""User-content clear lifecycle and concurrency tests for screenshot timeline."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from magi_plugin_sdk import UserContentClearContext, UserContentClearRequest
from magi_plugin_sdk.sensors import SensorSyncContext
from screenshot_timeline import sensor as sensor_module
from screenshot_timeline.sensor import ScreenshotSensor
from screenshot_timeline.storage_cleanup import UnsafeStoragePathError


class _RuntimePaths:
    def __init__(self, root: Path) -> None:
        self.root = root

    def plugin_cache_dir(self, plugin_id: str) -> Path:
        return self.root / plugin_id


class _FakeHelper:
    def __init__(
        self,
        *,
        block_capture: bool = False,
        late_write_after_timeout: bool = False,
    ) -> None:
        self.block_capture = block_capture
        self.late_write_after_timeout = late_write_after_timeout
        self.capture_started = asyncio.Event()
        self.capture_release = asyncio.Event()
        self.started = False
        self.shutdown_calls = 0
        self.capture_requests = 0
        self.late_write_tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.started = False
        self.capture_release.set()
        for task in self.late_write_tasks:
            task.cancel()
        if self.late_write_tasks:
            await asyncio.gather(*self.late_write_tasks, return_exceptions=True)

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = payload["op"]
        if operation == "probe_active_window":
            return {
                "id": payload["id"],
                "ok": True,
                "active_window": {
                    "app_bundle_id": "com.example.Private",
                    "app_name": "Private App",
                    "window_title": "Private Window",
                    "display_id": "primary",
                },
            }
        if operation == "probe_screen_lock":
            return {"id": payload["id"], "ok": True, "screen_locked": False}
        if operation != "capture_and_ocr":
            raise AssertionError(f"Unexpected helper operation: {operation}")

        self.capture_requests += 1
        self.capture_started.set()
        if self.late_write_after_timeout:
            paths = [Path(raw_path) for raw_path in payload["save_paths"].values()]

            async def _write_late() -> None:
                await asyncio.sleep(0.05)
                for path in paths:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"late private screenshot")

            self.late_write_tasks.append(asyncio.create_task(_write_late()))
            raise sensor_module.HelperTimeoutError("simulated helper timeout")
        if self.block_capture:
            await self.capture_release.wait()
        for raw_path in payload["save_paths"].values():
            path = Path(raw_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"private screenshot")
        return {
            "id": payload["id"],
            "ok": True,
            "captured_at": 1_775_000_000.0,
            "dimensions": [1200, 800],
            "ocr": {"text": "private text", "confidence_avg": 0.99},
            "phash": "0123456789abcdef",
        }


class _HelperFactory:
    def __init__(
        self,
        *,
        block_first_capture: bool = False,
        late_first_capture: bool = False,
    ) -> None:
        self.block_first_capture = block_first_capture
        self.late_first_capture = late_first_capture
        self.instances: list[_FakeHelper] = []

    def __call__(self, *, binary_argv: list[str]) -> _FakeHelper:
        _ = binary_argv
        helper = _FakeHelper(
            block_capture=self.block_first_capture and not self.instances,
            late_write_after_timeout=self.late_first_capture and not self.instances,
        )
        self.instances.append(helper)
        return helper


def _clear_context(tmp_path: Path) -> UserContentClearContext:
    return UserContentClearContext(
        request=UserContentClearRequest(clear_generation=11),
        runtime_paths=_RuntimePaths(tmp_path / "runtime"),
        plugin_id="screenshot_timeline",
        sensor_id="timeline.screenshot",
        plugin_settings={
            "sensors": {
                "screenshot_timeline": {
                    "enabled": True,
                    "active_window_interval_sec": 600,
                }
            }
        },
    )


def _sync_context(tmp_path: Path) -> SensorSyncContext:
    return SensorSyncContext(
        source_type="screenshot_timeline",
        manual=True,
        last_cursor="preserved-cursor",
        last_success_at=None,
        limit=10,
        runtime_paths=_RuntimePaths(tmp_path / "runtime"),
        plugin_settings={"sensors": {"screenshot_timeline": {}}},
    )


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    factory: _HelperFactory,
) -> None:
    monkeypatch.setattr(sensor_module, "HelperClient", factory)
    monkeypatch.setattr(sensor_module, "request_screen_recording", lambda: "granted")
    monkeypatch.setattr(sensor_module, "screen_recording_status", lambda: "granted")
    monkeypatch.setattr(sensor_module, "install_nsworkspace_observer", lambda _cb: None)


@pytest.mark.asyncio
async def test_clear_stops_runtime_erases_content_and_lazy_restarts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _HelperFactory()
    _patch_runtime(monkeypatch, factory)
    resources_root = tmp_path / "screenshots"
    session_db = tmp_path / "plugin-state" / "sessions.db"
    sensor = ScreenshotSensor(
        helper_argv=["fake-helper"],
        resources_root=resources_root,
        session_db_path=session_db,
        active_window_interval_sec=600,
        full_screen_interval_min=600,
    )
    await sensor.start()
    old_helper = factory.instances[0]
    old_orchestrator = sensor._orchestrator

    assert sensor._session_tracker is not None
    sensor._session_tracker.observe_capture(
        capture_id="old-capture",
        captured_at=1_775_000_000.0,
        app_bundle="com.example.Private",
        idle_seconds=0.0,
        screen_locked=False,
    )
    old_original = resources_root / "originals" / "2026" / "08" / "01" / "old.jpg"
    old_thumbnail = resources_root / "thumbnails" / "2026" / "08" / "01" / "old.jpg"
    old_legacy = resources_root / "2026" / "07" / "31" / "cap_OLD123_thumb.jpg"
    for path in (old_original, old_thumbnail, old_legacy):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"private")
    sensor._pending_items.append({"capture_id": "old-capture"})
    sensor._last_phash_by_window[("private", "window")] = "abcdef"
    sensor._guard.engage_panic(duration_seconds=600, now=100.0)

    context = _clear_context(tmp_path)
    await sensor.clear_user_content(context)
    await sensor.clear_user_content(context)

    assert old_helper.shutdown_calls == 1
    assert sensor._started is False
    assert sensor._capture_enabled is False
    assert sensor._active_timer is None
    assert sensor._full_screen_timer is None
    assert sensor._workspace_handle is None
    assert sensor._retention_task is None
    assert sensor._session_tracker is None
    assert sensor._session_store is None
    assert sensor._pending_items == []
    assert sensor._last_phash_by_window == {}
    assert sensor._guard.is_panic_active(now=101.0) is False
    assert not session_db.exists()
    assert not resources_root.joinpath("originals").exists()
    assert not resources_root.joinpath("thumbnails").exists()
    assert not old_legacy.exists()
    assert context.plugin_settings["sensors"]["screenshot_timeline"]["enabled"] is True
    assert all(not helper.started for helper in factory.instances)

    result = await sensor.collect_items(_sync_context(tmp_path))
    assert result.items == []
    assert sensor._started is True
    restarted_helper = factory.instances[-1]
    assert restarted_helper.started is True

    assert old_orchestrator is not None
    await old_orchestrator.emit("window_switch", now=10_000.0)
    assert restarted_helper.capture_requests == 0

    await sensor.trigger_once("manual")
    resumed_items = await sensor.drain_pending_items()
    assert len(resumed_items) == 1
    assert resumed_items[0]["ocr_text"] == "private text"
    await sensor.stop()


@pytest.mark.asyncio
async def test_clear_waits_for_inflight_capture_and_rejects_queued_old_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _HelperFactory(block_first_capture=True)
    _patch_runtime(monkeypatch, factory)
    resources_root = tmp_path / "screenshots"
    sensor = ScreenshotSensor(
        helper_argv=["fake-helper"],
        resources_root=resources_root,
        session_db_path=tmp_path / "plugin-state" / "sessions.db",
        capture_scope="active_window",
        active_window_interval_sec=600,
    )
    await sensor.start()
    helper = factory.instances[0]
    old_generation = sensor._capture_generation

    active = asyncio.create_task(
        sensor.trigger_once("timer", generation=old_generation)
    )
    await helper.capture_started.wait()
    queued = asyncio.create_task(
        sensor.trigger_once("window_switch", generation=old_generation)
    )
    await asyncio.sleep(0)

    clear_task = asyncio.create_task(
        sensor.clear_user_content(_clear_context(tmp_path))
    )
    await clear_task
    await active
    await queued

    assert helper.shutdown_calls == 1
    assert helper.capture_requests == 1
    assert sensor._active_capture_count == 0
    assert sensor._captures_idle.is_set()
    assert sensor._pending_items == []
    assert sensor._last_phash_by_window == {}
    assert not resources_root.joinpath("originals").exists()
    assert not resources_root.joinpath("thumbnails").exists()
    await asyncio.sleep(0.05)
    assert not list(resources_root.rglob("*.jpg"))


@pytest.mark.asyncio
async def test_clear_failure_from_symlinked_root_keeps_sensor_stopped_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _HelperFactory()
    _patch_runtime(monkeypatch, factory)
    external = tmp_path / "external"
    capture = external / "originals" / "2026" / "08" / "01" / "outside.jpg"
    capture.parent.mkdir(parents=True)
    capture.write_bytes(b"outside")
    resources_root = tmp_path / "screenshots"
    resources_root.symlink_to(external, target_is_directory=True)
    sensor = ScreenshotSensor(
        helper_argv=["fake-helper"],
        resources_root=resources_root,
        session_db_path=tmp_path / "plugin-state" / "sessions.db",
        active_window_interval_sec=600,
    )
    await sensor.start()

    with pytest.raises(UnsafeStoragePathError):
        await sensor.clear_user_content(_clear_context(tmp_path))

    assert sensor._started is False
    assert sensor._capture_enabled is False
    assert sensor._active_timer is None
    assert sensor._retention_task is None
    assert factory.instances[0].shutdown_calls == 1
    assert capture.read_bytes() == b"outside"

    resources_root.unlink()
    resources_root.mkdir()
    await sensor.clear_user_content(_clear_context(tmp_path))
    assert sensor._started is False
    assert capture.read_bytes() == b"outside"


@pytest.mark.asyncio
async def test_clear_kills_timed_out_helper_before_its_delayed_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _HelperFactory(late_first_capture=True)
    _patch_runtime(monkeypatch, factory)
    resources_root = tmp_path / "screenshots"
    sensor = ScreenshotSensor(
        helper_argv=["fake-helper"],
        resources_root=resources_root,
        session_db_path=tmp_path / "plugin-state" / "sessions.db",
        capture_scope="active_window",
        active_window_interval_sec=600,
    )
    await sensor.start()
    helper = factory.instances[0]

    await sensor.trigger_once("timer")
    assert helper.capture_started.is_set()
    assert helper.late_write_tasks

    await sensor.clear_user_content(_clear_context(tmp_path))
    await asyncio.sleep(0.1)

    assert helper.shutdown_calls == 1
    assert all(task.cancelled() for task in helper.late_write_tasks)
    assert sensor._pending_items == []
    assert not list(resources_root.rglob("*.jpg"))


@pytest.mark.asyncio
async def test_clear_attempts_every_storage_target_before_reporting_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    erased_resources: list[Path] = []

    def fail_database_erase(_db_path: Path) -> None:
        raise OSError("session database is busy")

    def record_resource_erase(resources_root: Path) -> None:
        erased_resources.append(resources_root)

    monkeypatch.setattr(sensor_module, "erase_session_database", fail_database_erase)
    monkeypatch.setattr(
        sensor_module,
        "erase_managed_screenshot_resources",
        record_resource_erase,
    )
    resources_root = tmp_path / "screenshots"
    sensor = ScreenshotSensor(
        resources_root=resources_root,
        session_db_path=tmp_path / "plugin-state" / "sessions.db",
    )

    with pytest.raises(OSError, match="session database is busy"):
        await sensor.clear_user_content(_clear_context(tmp_path))

    assert erased_resources == [resources_root]
    assert sensor._started is False
    assert sensor._capture_enabled is False
