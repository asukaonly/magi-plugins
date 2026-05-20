"""Integration test: sensor with mock helper, drive ticks, expect burst items."""
from __future__ import annotations

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
