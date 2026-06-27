"""Tests for the screenshot_timeline resolver tool + recall artifact projection."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load(name: str) -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"screenshot_timeline_{name}_test", module_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------- _date_subpath_from_capture_id ----------


def test_date_subpath_from_valid_capture_id() -> None:
    mod = _load("screenshot_tools")
    assert mod._date_subpath_from_capture_id("20260528T150647_328216_54AQ") == "2026/05/28"
    # Handles single-digit month/day with the zero-pad already in id format.
    assert mod._date_subpath_from_capture_id("20260101T000000_000000_AAAA") == "2026/01/01"


def test_date_subpath_rejects_malformed_id() -> None:
    mod = _load("screenshot_tools")
    assert mod._date_subpath_from_capture_id("") is None
    assert mod._date_subpath_from_capture_id("short") is None
    assert mod._date_subpath_from_capture_id("not_a_date_prefix_at_all") is None
    # Bad month
    assert mod._date_subpath_from_capture_id("20261332T100000_000000_AAAA") is None
    # Bad day
    assert mod._date_subpath_from_capture_id("20260532T100000_000000_AAAA") is None


# ---------- ScreenshotTimelineResolveCaptureRefsTool ----------


def _make_tool(tmp_path: Path):
    mod = _load("screenshot_tools")
    classes = mod.build_screenshot_timeline_tool_classes(resources_root=tmp_path)
    return classes[0]()


def _seed_capture(root: Path, capture_id: str, *, with_original: bool = True, with_thumb: bool = True) -> None:
    """Create the jpg fixture pair on disk."""
    date_subpath = "/".join([capture_id[0:4], capture_id[4:6], capture_id[6:8]])
    if with_original:
        orig = root / "originals" / date_subpath / f"{capture_id}.jpg"
        orig.parent.mkdir(parents=True, exist_ok=True)
        orig.write_bytes(b"FAKEJPEG_ORIG")
    if with_thumb:
        thumb = root / "thumbnails" / date_subpath / f"{capture_id}.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"FAKEJPEG_THUMB")


@pytest.mark.asyncio
async def test_resolve_returns_file_paths_for_existing_capture(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)
    _seed_capture(tmp_path, "20260528T150647_328216_54AQ")

    result = await tool.execute(
        {"capture_ref_ids": ["20260528T150647_328216_54AQ"]},
        context=None,  # type: ignore[arg-type]
    )
    assert result.success is True
    data = result.data
    assert data["resolved_count"] == 1
    assert len(data["file_paths"]) == 1
    assert data["file_paths"][0].endswith("originals/2026/05/28/20260528T150647_328216_54AQ.jpg")
    ref = data["asset_refs"][0]
    assert ref["asset_ref_id"] == "20260528T150647_328216_54AQ"
    assert ref["source_type"] == "screenshot_timeline"
    assert ref["resolver_tool"] == "screenshot_timeline_resolve_capture_refs"
    assert ref["resolution_state"] == "resolved"
    assert "original_path" in ref
    assert "thumbnail_path" in ref


@pytest.mark.asyncio
async def test_resolve_prefers_thumbnail_when_requested(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)
    _seed_capture(tmp_path, "20260528T150647_328216_54AQ")

    result = await tool.execute(
        {"capture_ref_ids": ["20260528T150647_328216_54AQ"], "prefer_thumbnail": True},
        context=None,  # type: ignore[arg-type]
    )
    assert result.success is True
    assert result.data["file_paths"][0].endswith(
        "thumbnails/2026/05/28/20260528T150647_328216_54AQ.jpg"
    )


@pytest.mark.asyncio
async def test_resolve_falls_back_to_thumbnail_when_original_gone(tmp_path: Path) -> None:
    """Retention deleted the original (it's been > 30 days). Tool should
    still return the thumbnail rather than reporting missing."""
    tool = _make_tool(tmp_path)
    _seed_capture(tmp_path, "20260528T150647_328216_54AQ", with_original=False)

    result = await tool.execute(
        {"capture_ref_ids": ["20260528T150647_328216_54AQ"]},
        context=None,  # type: ignore[arg-type]
    )
    assert result.success is True
    assert result.data["resolved_count"] == 1
    assert result.data["file_paths"][0].endswith(
        "thumbnails/2026/05/28/20260528T150647_328216_54AQ.jpg"
    )
    ref = result.data["asset_refs"][0]
    assert "original_path" not in ref       # gone — don't lie about it
    assert ref["thumbnail_path"].endswith(
        "thumbnails/2026/05/28/20260528T150647_328216_54AQ.jpg"
    )


@pytest.mark.asyncio
async def test_resolve_reports_missing_for_unknown_ids(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)
    # Nothing seeded.
    result = await tool.execute(
        {"capture_ref_ids": ["20260101T120000_000000_XXXX", "garbage"]},
        context=None,  # type: ignore[arg-type]
    )
    assert result.success is True
    assert result.data["resolved_count"] == 0
    assert set(result.data["missing_capture_ref_ids"]) == {
        "20260101T120000_000000_XXXX",
        "garbage",
    }


@pytest.mark.asyncio
async def test_resolve_rejects_empty_input(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)
    result = await tool.execute({"capture_ref_ids": []}, context=None)  # type: ignore[arg-type]
    assert result.success is False


# ---------- build_recall_asset_refs (projection hook) ----------


def test_recall_asset_refs_returns_resolver_pointer() -> None:
    mod = _load("screenshot_tools")
    event = {
        "event_id": "01HXYZ",
        "source": "screenshot_timeline",
        "source_item_id": "20260528T150647_328216_54AQ",
        "timestamp": 1779944807.0,
        "metadata_json": {
            "activity": {
                "qualifiers": {
                    "app_name": "Claude",
                    "window_title": "Claude — magi/插件改造",
                }
            },
            "activity_snapshot": {"provenance": {"captured_at": 1779944807.5}},
        },
    }
    refs = mod.build_recall_asset_refs(event)
    assert len(refs) == 1
    ref = refs[0]
    assert ref["asset_ref_id"] == "20260528T150647_328216_54AQ"
    assert ref["source_type"] == "screenshot_timeline"
    assert ref["resolver_tool"] == "screenshot_timeline_resolve_capture_refs"
    # Display name should combine app + window so the LLM can disambiguate.
    assert "Claude" in ref["display_name"]
    assert "magi" in ref["display_name"]


def test_recall_asset_refs_skips_non_screenshot_events() -> None:
    mod = _load("screenshot_tools")
    event = {
        "event_id": "01HABCDEF",
        "source": "chrome_history",
        "source_item_id": "page:123",
    }
    assert mod.build_recall_asset_refs(event) == []


def test_recall_asset_refs_skips_event_without_source_item_id() -> None:
    mod = _load("screenshot_tools")
    event = {"source": "screenshot_timeline", "source_item_id": ""}
    assert mod.build_recall_asset_refs(event) == []
