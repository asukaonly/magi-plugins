from __future__ import annotations

import asyncio
import importlib.util
import json
import time
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


def _load_source_module() -> ModuleType:
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "photo_library_source_apple_under_test"
    package = ModuleType(package_name)
    package.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.source",
        plugin_dir / "source.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _RuntimePaths:
    def plugin_cache_dir(self, plugin_id: str) -> Path:
        return Path("/tmp") / plugin_id


class _AppleReader:
    def scan_library(self, photos_library_path: str, *, limit: int, min_modified_at: float, **kwargs):
        assert photos_library_path == "/Photos Library.photoslibrary"
        assert limit >= 1000
        assert min_modified_at == 0.0
        item = {
            "asset_local_id": "apple-photos:UUID-1",
            "path": "/photos/IMG_0001.HEIC",
            "filename": "IMG_0001.HEIC",
            "extension": ".heic",
            "file_size": 1234,
            "file_hash": "hash-1",
            "modified_at": 1_710_000_100.0,
            "capture_timestamp": 1_710_000_000.0,
            "camera_make": "Apple",
            "camera_model": "iPhone",
            "lens_model": "back camera",
            "latitude": 31.2,
            "longitude": 121.4,
            "location_name": "Shanghai Disneyland",
            "location_source": "apple_photos",
            "apple_photos_place_name": "Shanghai Disneyland",
            "apple_photos_place_address": "Pudong, Shanghai",
        }
        return SimpleNamespace(items=[item], total_scanned=1, errors=0, has_more=False)


class _AutoLocateAppleReader(_AppleReader):
    def scan_library(self, photos_library_path: str, *, limit: int, min_modified_at: float, **kwargs):
        assert photos_library_path == ""
        return super().scan_library(
            "/Photos Library.photoslibrary",
            limit=limit,
            min_modified_at=min_modified_at,
            **kwargs,
        )


class _PagedAppleReader(_AppleReader):
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def scan_library(self, photos_library_path: str, *, limit: int, min_modified_at: float, **kwargs):
        self.calls.append(
            {
                "photos_library_path": photos_library_path,
                "limit": limit,
                "min_modified_at": min_modified_at,
                **kwargs,
            }
        )
        assert kwargs["order_by"] == "capture_timestamp"
        assert kwargs["descending"] is True
        assert kwargs["capture_before"] is None
        result = super().scan_library(
            photos_library_path,
            limit=limit,
            min_modified_at=min_modified_at,
            **kwargs,
        )
        result.has_more = True
        return result


class _CustomRangeAppleReader(_AppleReader):
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def scan_library(self, photos_library_path: str, *, limit: int, min_modified_at: float, **kwargs):
        self.calls.append(
            {
                "photos_library_path": photos_library_path,
                "limit": limit,
                "min_modified_at": min_modified_at,
                **kwargs,
            }
        )
        assert kwargs["order_by"] == "capture_timestamp"
        assert kwargs["descending"] is True
        assert time.strftime("%Y-%m-%d", time.localtime(float(kwargs["capture_after"]))) == "2024-06-15"
        assert time.strftime("%Y-%m-%d", time.localtime(float(kwargs["capture_before"]))) == "2024-06-16"
        return super().scan_library(
            photos_library_path,
            limit=limit,
            min_modified_at=min_modified_at,
            **kwargs,
        )


def test_apple_photos_mode_does_not_require_source_paths() -> None:
    mod = _load_source_module()
    source = mod.PhotoLibraryTimelineSource(
        source_type="photo_library_apple_photos",
        source_mode="apple_photos",
        photos_library_path="/Photos Library.photoslibrary",
        apple_reader=_AppleReader(),
        analysis_features=[],
        settle_window_seconds=0,
    )

    result = asyncio.run(
        source.collect_items(
            mod.SourceSyncContext(
        connection_id="test-connection",
                source_type="photo_library_apple_photos",
                manual=True,
                last_cursor=None,
                last_success_at=None,
                limit=200,
                runtime_paths=_RuntimePaths(),
                plugin_settings={
                    "sources": {
                        "photo_library_apple_photos": {
                            "source_mode": "apple_photos",
                            "photos_library_path": "/Photos Library.photoslibrary",
                            "analysis_features": [],
                        }
                    }
                },
            )
        )
    )

    assert result.stats["source_mode"] == "apple_photos"
    assert result.stats["photos_seen"] == 1


def test_apple_photos_place_name_wins_over_geocode(monkeypatch) -> None:
    mod = _load_source_module()
    source = mod.PhotoLibraryTimelineSource(
        source_type="photo_library_apple_photos",
        source_mode="apple_photos",
        photos_library_path="/Photos Library.photoslibrary",
        apple_reader=_AppleReader(),
        analysis_features=["geocode"],
        settle_window_seconds=0,
    )
    monkeypatch.setattr(
        mod,
        "_geo_batch_lookup",
        lambda _coords, _cache_dir: [SimpleNamespace(country_code="CN")],
    )
    monkeypatch.setattr(mod, "format_location", lambda _geo, locale_map=None: "Shanghai")

    result = asyncio.run(
        source.collect_items(
            mod.SourceSyncContext(
        connection_id="test-connection",
                source_type="photo_library_apple_photos",
                manual=True,
                last_cursor=None,
                last_success_at=None,
                limit=200,
                runtime_paths=_RuntimePaths(),
                plugin_settings={
                    "sources": {
                        "photo_library_apple_photos": {
                            "source_mode": "apple_photos",
                            "photos_library_path": "/Photos Library.photoslibrary",
                            "analysis_features": ["geocode"],
                        }
                    }
                },
            )
        )
    )

    item = [change.payload for change in result.changes][0]
    assert item["location_name"] == "Shanghai Disneyland"
    assert item["location_source"] == "apple_photos"
    assert item["apple_photos_place_name"] == "Shanghai Disneyland"
    assert len([change.payload for change in result.changes]) == 1
    assert [change.payload for change in result.changes][0]["representative_photos"][0]["asset_local_id"] == "apple-photos:UUID-1"


def test_apple_photos_mode_auto_locates_library_when_path_is_unset() -> None:
    mod = _load_source_module()
    source = mod.PhotoLibraryTimelineSource(
        source_type="photo_library_apple_photos",
        source_mode="apple_photos",
        photos_library_path="",
        apple_reader=_AutoLocateAppleReader(),
        analysis_features=[],
        settle_window_seconds=0,
    )

    result = asyncio.run(
        source.collect_items(
            mod.SourceSyncContext(
        connection_id="test-connection",
                source_type="photo_library_apple_photos",
                manual=True,
                last_cursor=None,
                last_success_at=None,
                limit=200,
                runtime_paths=_RuntimePaths(),
                plugin_settings={
                    "sources": {
                        "photo_library_apple_photos": {
                            "source_mode": "apple_photos",
                            "analysis_features": [],
                        }
                    }
                },
            )
        )
    )

    assert result.stats["source_mode"] == "apple_photos"
    assert result.stats["photos_seen"] == 1


def test_apple_photos_initial_backfill_pages_by_capture_time() -> None:
    mod = _load_source_module()
    apple_reader = _PagedAppleReader()
    source = mod.PhotoLibraryTimelineSource(
        source_type="photo_library_apple_photos",
        source_mode="apple_photos",
        photos_library_path="/Photos Library.photoslibrary",
        apple_reader=apple_reader,
        analysis_features=[],
        settle_window_seconds=0,
    )

    result = asyncio.run(
        source.collect_items(
            mod.SourceSyncContext(
        connection_id="test-connection",
                source_type="photo_library_apple_photos",
                manual=True,
                last_cursor=None,
                last_success_at=None,
                limit=200,
                runtime_paths=_RuntimePaths(),
                plugin_settings={
                    "sources": {
                        "photo_library_apple_photos": {
                            "source_mode": "apple_photos",
                            "photos_library_path": "/Photos Library.photoslibrary",
                            "analysis_features": [],
                        }
                    }
                },
            )
        )
    )

    assert result.stats["has_more"] is True
    assert result.stats["cursor_kind"] == "opaque"
    cursor = json.loads(result.next_cursor or "")
    assert cursor["mode"] == "backfill"
    assert cursor["capture_before"] == 1_710_000_000.0
    assert apple_reader.calls[0]["order_by"] == "capture_timestamp"


def test_apple_photos_custom_range_backfill_uses_capture_bounds() -> None:
    mod = _load_source_module()
    apple_reader = _CustomRangeAppleReader()
    source = mod.PhotoLibraryTimelineSource(
        source_type="photo_library_apple_photos",
        source_mode="apple_photos",
        photos_library_path="/Photos Library.photoslibrary",
        apple_reader=apple_reader,
        analysis_features=[],
        settle_window_seconds=0,
    )

    result = asyncio.run(
        source.collect_items(
            mod.SourceSyncContext(
        connection_id="test-connection",
                source_type="photo_library_apple_photos",
                manual=True,
                last_cursor=None,
                last_success_at=None,
                limit=200,
                runtime_paths=_RuntimePaths(),
                plugin_settings={
                    "sources": {
                        "photo_library_apple_photos": {
                            "source_mode": "apple_photos",
                            "photos_library_path": "/Photos Library.photoslibrary",
                            "analysis_features": [],
                            "initial_sync_policy": "custom_range",
                            "initial_sync_start_date": "2024-06-15",
                            "initial_sync_end_date": "2024-06-15",
                        }
                    }
                },
            )
        )
    )

    assert result.stats["has_more"] is False
    assert result.stats["sync_phase"] == "backfill"
    assert (
        float(apple_reader.calls[0]["capture_before"])
        - float(apple_reader.calls[0]["capture_after"])
    ) == 86400.0


def test_apple_photos_rejects_old_timestamp_cursor() -> None:
    import pytest
    from photo_library_core.source import _decode_apple_photos_cursor
    with pytest.raises(ValueError, match="Unsupported Apple Photos"):
        _decode_apple_photos_cursor("1710000000")
