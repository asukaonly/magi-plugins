"""Integration test: sensor with mock helper, drive ticks, expect burst items."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mock_helper.py"


def _load(name: str) -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"screenshot_timeline_{name}", module_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_capture_tick_produces_burst_items_via_flush(tmp_path: Path) -> None:
    sensor_mod = _load("sensor")
    sensor = sensor_mod.ScreenshotSensor(
        helper_argv=[sys.executable, str(_FIXTURE)],
        resources_root=tmp_path,
        gap_minutes=5,
        max_minutes=30,
        retention_days=30,
    )
    await sensor.start()
    try:
        await sensor.trigger_once("timer")
        await sensor.trigger_once("timer")
        items = await sensor.flush_pending_bursts()
        assert len(items) == 1
        item = items[0]
        # Validate the burst-dict shape that build_output will consume.
        # Mock helper returns captured_at=1700000000.0 → 2023-11-14 UTC.
        assert item["source_item_id"].startswith("20231114_")
        assert item["capture_count"] == 2
        assert item["app_bundle"] == "com.apple.Safari"
        assert "[truncated]" not in item["ocr_text_union"]

        # Verify build_output produces a valid SensorOutput.
        output = await sensor.build_output(item)
        assert output.source_type == "screenshot_timeline"
        assert output.source_item_id == item["source_item_id"]
        assert output.narration.body  # OCR text union
        assert output.activity.source.code == "screenshot_timeline"
        # qualifiers are stringified by SensorBase._build_activity
        assert output.activity.qualifiers["capture_count"] == "2"
    finally:
        await sensor.stop()


@pytest.mark.asyncio
async def test_blocked_app_skips_capture(tmp_path: Path) -> None:
    sensor_mod = _load("sensor")
    sensor = sensor_mod.ScreenshotSensor(
        helper_argv=[sys.executable, str(_FIXTURE)],
        resources_root=tmp_path,
        gap_minutes=5,
        max_minutes=30,
        retention_days=30,
        extra_app_blocklist=("com.apple.Safari",),  # mock helper always reports Safari
    )
    await sensor.start()
    try:
        await sensor.trigger_once("timer")
        items = await sensor.flush_pending_bursts()
        assert items == []
    finally:
        await sensor.stop()


@pytest.mark.asyncio
async def test_sensor_active_window_timer_fires_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a short interval, the active-window timer should drive at least one capture."""
    sensor_mod = _load("sensor")
    # Force the permission probe to report "granted" so start() actually wires up
    # the orchestrator + timer. The real probe now spawns the Swift helper binary,
    # which has its own per-binary TCC entry that may not be granted on dev/CI machines.
    monkeypatch.setattr(sensor_mod, "screen_recording_status", lambda: "granted")
    monkeypatch.setattr(sensor_mod, "request_screen_recording", lambda: "granted")
    sensor = sensor_mod.ScreenshotSensor(
        helper_argv=[sys.executable, str(_FIXTURE)],
        resources_root=tmp_path,
        gap_minutes=5,
        max_minutes=30,
        retention_days=30,
        active_window_interval_sec=0.05,    # fast tick for test
        full_screen_interval_min=999.0,      # disable in test (also: scope=active_window keeps it off)
        capture_scope="active_window",
    )
    await sensor.start()
    try:
        await asyncio.sleep(0.25)  # ~3-5 timer ticks
    finally:
        # Force-flush any open burst before stop so we can assert on it
        items = await sensor.flush_pending_bursts()
        await sensor.stop()
    assert len(items) >= 1, "expected at least one burst from timer-driven captures"
    assert items[0]["capture_count"] >= 1


@pytest.mark.asyncio
async def test_collect_items_does_not_force_close_open_burst(tmp_path: Path) -> None:
    """collect_items must NOT prematurely close a burst that's still accumulating.

    A naive flush_all-on-every-poll would chop a long reading session into many
    small bursts. The real-time loop should harvest only naturally-closed bursts.
    """
    sensor_mod = _load("sensor")
    sensor = sensor_mod.ScreenshotSensor(
        helper_argv=[sys.executable, str(_FIXTURE)],
        resources_root=tmp_path,
        gap_minutes=5,
        max_minutes=30,
        retention_days=30,
    )
    await sensor.start()
    try:
        # Two captures of the same window — burst still OPEN
        await sensor.trigger_once("timer")
        await sensor.trigger_once("timer")

        # Production pull-sync poll: should return 0 items because the burst hasn't closed
        result = await sensor.collect_items(context=None)  # type: ignore[arg-type]
        assert result.items == []

        # Force-flush (e.g. on shutdown) returns the 1 burst
        items = await sensor.flush_pending_bursts()
        assert len(items) == 1
        assert items[0]["capture_count"] == 2
    finally:
        await sensor.stop()


@pytest.mark.asyncio
async def test_start_is_skipped_when_screen_recording_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Screen Recording is denied, start() must not spawn the helper.

    macOS caches a denial — re-running the helper just produces a stream of
    PERMISSION_DENIED errors, so we refuse to start and leave the sensor idle
    until the user grants permission in System Settings and toggles the source
    again.
    """
    sensor_mod = _load("sensor")
    monkeypatch.setattr(sensor_mod, "screen_recording_status", lambda: "denied")

    sensor = sensor_mod.ScreenshotSensor(
        helper_argv=[sys.executable, str(_FIXTURE)],
        resources_root=tmp_path,
        gap_minutes=5,
        max_minutes=30,
        retention_days=30,
    )
    await sensor.start()
    try:
        # Orchestrator/timers/retention task must NOT be wired up if start() bailed
        assert sensor._orchestrator is None
        assert sensor._active_timer is None
        assert sensor._full_screen_timer is None
        assert sensor._retention_task is None
        # Helper subprocess must not have been started
        assert sensor._helper is not None and sensor._helper._proc is None
    finally:
        await sensor.stop()
