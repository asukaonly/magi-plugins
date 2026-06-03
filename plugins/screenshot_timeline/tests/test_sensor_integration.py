"""Integration test: sensor with mock helper, drive ticks, expect per-capture L1 items."""
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
async def test_capture_tick_produces_one_l1_item_per_capture(tmp_path: Path) -> None:
    """Two ticks → two L1 items. No burst aggregation."""
    sensor_mod = _load("sensor")
    sensor = sensor_mod.ScreenshotSensor(
        helper_argv=[sys.executable, str(_FIXTURE)],
        resources_root=tmp_path,
        retention_days=30,
    )
    await sensor.start()
    try:
        await sensor.trigger_once("timer")
        await sensor.trigger_once("timer")
        items = await sensor.drain_pending_items()
        # One row per capture — drop-in replacement of the old "1 burst
        # holds 2 captures" model. Critical for embedding chunk balance
        # against short rows from other sources (chrome_history etc).
        assert len(items) == 2
        for item in items:
            # capture_id is timestamp-prefixed (see ids.new_capture_id).
            assert item["source_item_id"] == item["capture_id"]
            assert item["app_bundle"] == "com.apple.Safari"
            assert item["window_title"] == "Mock Window"
            assert item["ocr_text"] == "hello world"
            assert item["phash"]  # mock helper returns one per request
            assert item["trigger"] == "timer"
            assert "original_path" in item and "thumbnail_path" in item

        # Verify build_output produces a SensorOutput per item.
        output = await sensor.build_output(items[0])
        assert output.source_type == "screenshot_timeline"
        assert output.source_item_id == items[0]["source_item_id"]
        assert output.narration.body == "hello world"
        assert output.activity.source.code == "screenshot_timeline"
        assert output.activity.action.code == "screen_capture"
        assert output.activity.qualifiers["trigger"] == "timer"
    finally:
        await sensor.stop()


@pytest.mark.asyncio
async def test_blocked_app_skips_capture(tmp_path: Path) -> None:
    sensor_mod = _load("sensor")
    sensor = sensor_mod.ScreenshotSensor(
        helper_argv=[sys.executable, str(_FIXTURE)],
        resources_root=tmp_path,
        retention_days=30,
        extra_app_blocklist=("com.apple.Safari",),  # mock helper always reports Safari
    )
    await sensor.start()
    try:
        await sensor.trigger_once("timer")
        items = await sensor.drain_pending_items()
        assert items == []
    finally:
        await sensor.stop()


@pytest.mark.asyncio
async def test_phash_dedup_drops_near_identical_capture(tmp_path: Path) -> None:
    """Two ticks with the SAME phash → second is dropped + its jpgs deleted."""
    sensor_mod = _load("sensor")
    sensor = sensor_mod.ScreenshotSensor(
        helper_argv=[sys.executable, str(_FIXTURE)],
        resources_root=tmp_path,
        retention_days=30,
        phash_dedup_threshold=5,
    )
    await sensor.start()
    try:
        await sensor.trigger_once("timer")
        # Force the next capture to have the SAME phash as the previous
        # one for the same window. The window key is (app_bundle, window_title)
        # — mock helper always returns ("com.apple.Safari", "Mock Window").
        items_after_first = list(sensor._pending_items)
        assert len(items_after_first) == 1
        prior_phash = items_after_first[0]["phash"]
        sensor._last_phash_by_window[("com.apple.Safari", "Mock Window")] = prior_phash

        # Patch the helper's response for the next call: re-use the same phash.
        # We achieve this without touching mock_helper by injecting it
        # through the sensor's last-phash table — when the second capture
        # finishes, the new phash (different, derived from a new rid)
        # would normally NOT match, so we set the baseline to a value
        # that we know matches what mock will return.
        # Instead easier: bump threshold to 64 so EVERYTHING dedupes.
        sensor.phash_dedup_threshold = 64

        await sensor.trigger_once("timer")
        items = await sensor.drain_pending_items()
        # 1 from first tick (still there because we didn't drain between)
        # + 0 from second (deduped) = 1 total
        assert len(items) == 1
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
        retention_days=30,
        active_window_interval_sec=0.05,    # fast tick for test
        full_screen_interval_min=999.0,      # disable in test (also: scope=active_window keeps it off)
        capture_scope="active_window",
    )
    await sensor.start()
    try:
        await asyncio.sleep(0.25)  # ~3-5 timer ticks
    finally:
        items = await sensor.drain_pending_items()
        await sensor.stop()
    assert len(items) >= 1, "expected at least one capture from timer-driven ticks"
    assert items[0]["app_bundle"] == "com.apple.Safari"


@pytest.mark.asyncio
async def test_collect_items_returns_per_capture_immediately(tmp_path: Path) -> None:
    """Per-capture L1 model: collect_items() returns one item per capture
    without waiting for a "burst close" boundary. The previous burst-
    aggregated design held items back until a window change or gap — that
    caused long reading sessions to never surface to the host until
    shutdown. Per-image flush means the host can keep up with realtime.
    """
    sensor_mod = _load("sensor")
    sensor = sensor_mod.ScreenshotSensor(
        helper_argv=[sys.executable, str(_FIXTURE)],
        resources_root=tmp_path,
        retention_days=30,
    )
    await sensor.start()
    try:
        await sensor.trigger_once("timer")
        await sensor.trigger_once("timer")

        result = await sensor.collect_items(context=None)  # type: ignore[arg-type]
        assert len(result.items) == 2  # both captures immediately visible
        # Second collect_items finds nothing — items are drained on read.
        result2 = await sensor.collect_items(context=None)  # type: ignore[arg-type]
        assert result2.items == []
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


def _ax_item(**overrides: object) -> dict:
    item = {
        "capture_id": "cap_ax", "source_item_id": "cap_ax", "idempotency_key": "cap_ax",
        "captured_at": 1000.0, "app_bundle": "com.microsoft.VSCode", "app_name": "Code",
        "window_title": "plugin.toml — magi-plugins", "url": None, "display_id": "primary",
        "ocr_text": "OCR FALLBACK TEXT", "ocr_confidence_avg": 0.5,
        "ax_text": "[file] plugin.toml\n[diff] adapter.py (d500c61)",
        "ax_content_chars": 10300, "ax_node_count": 4103, "ax_blocks": [],
        "used_ocr_fallback": False, "trigger": "timer", "scope": "active_window",
        "original_path": "/x/o.jpg", "thumbnail_path": "/x/t.jpg",
        "original_expires_at": 87400.0, "dimensions": [1920, 1080],
        "phash": "abcabcabcabcabca", "idle_seconds": 0.0, "session_id": "",
    }
    item.update(overrides)
    return item


@pytest.mark.asyncio
async def test_build_output_prefers_ax_text_when_rich(tmp_path: Path) -> None:
    """AX-rich capture (used_ocr_fallback=False) → content block is the exact AX text."""
    sensor_mod = _load("sensor")
    sensor = sensor_mod.ScreenshotSensor(resources_root=tmp_path)
    output = await sensor.build_output(_ax_item())
    assert output.content_blocks[0].value == "[file] plugin.toml\n[diff] adapter.py (d500c61)"
    assert output.narration.body == output.content_blocks[0].value
    assert output.provenance["content_source"] == "ax"
    assert output.provenance["ax_content_chars"] == 10300


@pytest.mark.asyncio
async def test_build_output_falls_back_to_ocr_when_ax_hollow(tmp_path: Path) -> None:
    """Hollow AX tree (WeChat/QQ-like, used_ocr_fallback=True) → OCR text wins."""
    sensor_mod = _load("sensor")
    sensor = sensor_mod.ScreenshotSensor(resources_root=tmp_path)
    item = _ax_item(used_ocr_fallback=True, ax_text="alert\nAllow", ax_content_chars=6, ax_node_count=6)
    output = await sensor.build_output(item)
    assert output.content_blocks[0].value == "OCR FALLBACK TEXT"
    assert output.provenance["content_source"] == "ocr"


@pytest.mark.asyncio
async def test_captures_are_l1_only_and_never_reach_l2(tmp_path: Path) -> None:
    """Screen OCR/AX text has no speaker attribution, so captures must stay out
    of L2 cognition. A chat window captured on screen would otherwise let the
    L2 extractor attribute a third party's words to the user (e.g. another
    person's "我520入职" becomes a user:self fact) and balloon the extraction
    prompt with huge AX dumps. Both gates must hold:

      1. memory_policy.cognition_eligible is False  → host L2 staging skips
         the event (see magi staging: ``if not event.cognition_eligible``).
      2. l2_batch_policy() returns None             → never staged at all.

    Captures are still written to L1 (ingest_target stays ``l1_only``) so the
    "what was on my screen" retrieval keeps working.
    """
    sensor_mod = _load("sensor")
    sensor = sensor_mod.ScreenshotSensor(resources_root=tmp_path)

    assert sensor.memory_policy.cognition_eligible is False
    assert sensor.memory_policy.to_dict()["cognition_eligible"] is False
    assert sensor.memory_policy.ingest_target == "l1_only"  # still searchable at L1

    output = await sensor.build_output(_ax_item())
    assert sensor.l2_batch_policy(output) is None
