from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


def _load_sensor_module() -> ModuleType:
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "photo_library_sensor_apple_under_test"
    package = ModuleType(package_name)
    package.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.sensor",
        plugin_dir / "sensor.py",
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
    def scan_library(self, photos_library_path: str, *, limit: int, min_modified_at: float):
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
            "location_name": "Shanghai",
        }
        return SimpleNamespace(items=[item], total_scanned=1, errors=0)


def test_apple_photos_mode_does_not_require_source_paths() -> None:
    mod = _load_sensor_module()
    sensor = mod.PhotoLibraryTimelineSensor(
        source_mode="apple_photos",
        photos_library_path="/Photos Library.photoslibrary",
        apple_reader=_AppleReader(),
        analysis_features=[],
        settle_window_seconds=0,
    )

    result = asyncio.run(
        sensor.collect_items(
            mod.SensorSyncContext(
                source_type="photo_library",
                manual=True,
                last_cursor=None,
                last_success_at=None,
                limit=200,
                runtime_paths=_RuntimePaths(),
                plugin_settings={
                    "sensors": {
                        "photo_library": {
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
    assert len(result.items) == 1
    assert result.items[0]["representative_photos"][0]["asset_local_id"] == "apple-photos:UUID-1"
