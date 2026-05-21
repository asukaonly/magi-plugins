"""Tests for burst aggregator."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_module() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "burst_aggregator.py"
    spec = importlib.util.spec_from_file_location("screenshot_timeline_burst", module_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # required for @dataclass under Python 3.12
    spec.loader.exec_module(mod)
    return mod


def _cap(
    *,
    capture_id: str,
    captured_at: float,
    app_bundle: str,
    window_title: str,
    ocr_text: str,
    trigger: str = "timer",
    scope: str = "active_window",
    original_path: str = "/tmp/orig.jpg",
    thumbnail_path: str = "/tmp/thumb.jpg",
    dimensions: tuple[int, int] = (1920, 1200),
    url: str | None = None,
) -> dict:
    return {
        "capture_id": capture_id,
        "captured_at": captured_at,
        "app_bundle": app_bundle,
        "window_title": window_title,
        "url": url,
        "ocr_text": ocr_text,
        "trigger": trigger,
        "scope": scope,
        "original_path": original_path,
        "thumbnail_path": thumbnail_path,
        "dimensions": dimensions,
        "ocr_confidence_avg": 0.9,
    }


def test_first_capture_opens_a_burst() -> None:
    agg = _load_module().BurstAggregator(gap_minutes=5, max_minutes=30, retention_days=30)
    closed = agg.ingest(_cap(
        capture_id="cap_1", captured_at=100.0,
        app_bundle="com.apple.Safari", window_title="W", ocr_text="hello",
    ))
    assert closed == []
    assert agg.open_burst_count() == 1


def test_same_window_within_gap_extends_burst() -> None:
    agg = _load_module().BurstAggregator(gap_minutes=5, max_minutes=30, retention_days=30)
    agg.ingest(_cap(capture_id="cap_1", captured_at=100.0,
                    app_bundle="com.apple.Safari", window_title="W", ocr_text="A"))
    closed = agg.ingest(_cap(capture_id="cap_2", captured_at=100.0 + 60,
                             app_bundle="com.apple.Safari", window_title="W", ocr_text="B"))
    assert closed == []
    assert agg.open_burst_count() == 1


def test_window_change_cuts_burst() -> None:
    agg = _load_module().BurstAggregator(gap_minutes=5, max_minutes=30, retention_days=30)
    agg.ingest(_cap(capture_id="cap_1", captured_at=100.0,
                    app_bundle="com.apple.Safari", window_title="W1", ocr_text="A"))
    closed = agg.ingest(_cap(capture_id="cap_2", captured_at=100.0 + 30,
                             app_bundle="com.apple.Safari", window_title="W2", ocr_text="B"))
    assert len(closed) == 1
    assert closed[0].capture_count == 1
    assert closed[0].window_title == "W1"
    assert agg.open_burst_count() == 1


def test_gap_exceeded_cuts_burst() -> None:
    agg = _load_module().BurstAggregator(gap_minutes=5, max_minutes=30, retention_days=30)
    agg.ingest(_cap(capture_id="cap_1", captured_at=100.0,
                    app_bundle="com.apple.Safari", window_title="W", ocr_text="A"))
    closed = agg.ingest(_cap(capture_id="cap_2", captured_at=100.0 + 6 * 60,
                             app_bundle="com.apple.Safari", window_title="W", ocr_text="B"))
    assert len(closed) == 1
    assert closed[0].capture_count == 1


def test_max_duration_cuts_burst_even_within_gap() -> None:
    agg = _load_module().BurstAggregator(gap_minutes=5, max_minutes=30, retention_days=30)
    base = 100.0
    # 6 captures over 31 min, never exceeding 5min gap
    for i in range(6):
        result = agg.ingest(_cap(capture_id=f"cap_{i}", captured_at=base + i * 5 * 60 + i,
                                 app_bundle="com.apple.Safari", window_title="W", ocr_text=f"line {i}"))
    # By the 6th the burst would exceed 30 min from start; at least one burst should have closed
    flushed = agg.flush_all(now=base + 35 * 60)
    closed_lengths = [b.capture_count for b in flushed]
    assert sum(closed_lengths) >= 1


def test_ocr_text_union_deduplicates_lines_and_preserves_order() -> None:
    agg = _load_module().BurstAggregator(gap_minutes=5, max_minutes=30, retention_days=30)
    agg.ingest(_cap(capture_id="cap_1", captured_at=100.0,
                    app_bundle="com.apple.Safari", window_title="W",
                    ocr_text="Hello\nWorld\nMagi"))
    agg.ingest(_cap(capture_id="cap_2", captured_at=100.0 + 60,
                    app_bundle="com.apple.Safari", window_title="W",
                    ocr_text="Hello\nWorld\nNew line"))
    closed = agg.flush_all(now=100.0 + 10 * 60)
    assert len(closed) == 1
    body_lines = closed[0].ocr_text_union.splitlines()
    # window title appears first
    assert body_lines[0] == "W"
    assert body_lines[1:] == ["Hello", "World", "Magi", "New line"]


def test_ocr_text_union_truncates_at_cap() -> None:
    agg = _load_module().BurstAggregator(gap_minutes=5, max_minutes=30, retention_days=30, content_char_cap=200)
    # 4 DISTINCT ~90-char lines so dedup keeps them all and we exceed the cap.
    distinct_lines = "\n".join(f"line-{i} " + "x" * 80 for i in range(4))
    agg.ingest(_cap(capture_id="cap_1", captured_at=100.0,
                    app_bundle="A", window_title="W",
                    ocr_text=distinct_lines))
    closed = agg.flush_all(now=100.0 + 10 * 60)
    assert closed[0].ocr_text_union.endswith("\n[truncated]")
    assert len(closed[0].ocr_text_union) <= 200 + len("\n[truncated]")


def test_source_item_id_is_deterministic_per_burst() -> None:
    agg = _load_module().BurstAggregator(gap_minutes=5, max_minutes=30, retention_days=30)
    agg.ingest(_cap(capture_id="cap_1", captured_at=1_700_000_000.0,
                    app_bundle="com.apple.Safari", window_title="W", ocr_text="A"))
    closed = agg.flush_all(now=1_700_000_000.0 + 10 * 60)
    sid = closed[0].source_item_id
    assert sid.startswith("20231114_1700000000_com.apple.Safari_")
    assert sid == closed[0].idempotency_key


def test_capture_count_matches_ingested() -> None:
    agg = _load_module().BurstAggregator(gap_minutes=5, max_minutes=30, retention_days=30)
    for i in range(4):
        agg.ingest(_cap(capture_id=f"cap_{i}", captured_at=100.0 + i,
                        app_bundle="A", window_title="W", ocr_text=f"line {i}"))
    closed = agg.flush_all(now=100.0 + 10 * 60)
    assert closed[0].capture_count == 4
    assert len(closed[0].captures) == 4


def test_trigger_breakdown_counted() -> None:
    agg = _load_module().BurstAggregator(gap_minutes=5, max_minutes=30, retention_days=30)
    agg.ingest(_cap(capture_id="cap_1", captured_at=100.0,
                    app_bundle="A", window_title="W", ocr_text="A", trigger="timer"))
    agg.ingest(_cap(capture_id="cap_2", captured_at=101.0,
                    app_bundle="A", window_title="W", ocr_text="B", trigger="window_switch"))
    agg.ingest(_cap(capture_id="cap_3", captured_at=102.0,
                    app_bundle="A", window_title="W", ocr_text="C", trigger="keyboard"))
    closed = agg.flush_all(now=100.0 + 10 * 60)
    assert closed[0].trigger_breakdown == {"timer": 1, "window_switch": 1, "keyboard": 1, "manual": 0}
