from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType

from magi_plugin_sdk.sensors import SensorSyncContext


def _load_sensor_class():
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "chrome_history_sensor_under_test"
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
    return module.ChromeHistoryTimelineSensor


def test_chrome_history_output_uses_domain_promotion_key() -> None:
    sensor_cls = _load_sensor_class()
    sensor = sensor_cls()

    output = asyncio.run(
        sensor.build_output(
            {
                "visit_id": 42,
                "url": "https://example.com/docs",
                "canonical_url": "https://example.com/docs",
                "domain": "example.com",
                "title": "Example docs",
                "visit_time": 1_710_000_000.0,
                "merged_visit_count": 3,
            }
        )
    )

    assert output.domain_payload["promotion_key"] == "example.com"


def test_chrome_history_output_includes_source_facets() -> None:
    sensor_cls = _load_sensor_class()
    sensor = sensor_cls()

    output = asyncio.run(
        sensor.build_output(
            {
                "visit_id": 42,
                "url": "https://example.com/docs",
                "canonical_url": "https://example.com/docs",
                "domain": "example.com",
                "title": "Example docs",
                "visit_time": 1_710_000_000.0,
                "merged_visit_count": 3,
            }
        )
    )

    facets = output.domain_payload["source_facets"]
    assert {"name": "browser.domain", "text": "example.com"} in facets
    assert {"name": "browser.title", "text": "Example docs"} in facets
    assert {"name": "browser.url", "text": "https://example.com/docs"} in facets
    assert {"name": "browser.visit_count", "numeric": 3} in facets


class _RuntimePaths:
    pass


class _Reader:
    def __init__(self) -> None:
        self.kwargs = {}

    def read_visits(self, *, limit: int, **kwargs):
        self.kwargs = {"limit": limit, **kwargs}
        return [
            {
                "visit_id": str(index),
                "last_visit_id": str(index),
                "visit_time": 1_710_000_000.0 + index,
                "canonical_url": f"https://example.com/{index}",
                "url": f"https://example.com/{index}",
                "domain": "example.com",
                "title": f"Page {index}",
                "merged_visit_count": 1,
            }
            for index in range(1, limit + 1)
        ]


class _ClearableReader(_Reader):
    def __init__(self) -> None:
        super().__init__()
        self.cleared_root: Path | None = None

    def clear_temp_copies(self, temp_root: Path) -> None:
        self.cleared_root = temp_root


class _PluginRuntimePaths:
    def __init__(self, root: Path) -> None:
        self.root = root

    def plugin_cache_dir(self, plugin_id: str) -> Path:
        return self.root / plugin_id


def test_chrome_history_custom_range_uses_local_day_bounds_and_continues() -> None:
    sensor_cls = _load_sensor_class()
    reader = _Reader()
    sensor = sensor_cls(reader=reader)

    result = asyncio.run(
        sensor.collect_items(
            SensorSyncContext(
                source_type="chrome_history",
                manual=True,
                last_cursor=None,
                last_success_at=None,
                limit=2,
                runtime_paths=_RuntimePaths(),
                plugin_settings={
                    "sensors": {
                        "chrome_history": {
                            "initial_sync_policy": "custom_range",
                            "initial_sync_start_date": "2026-06-01",
                            "initial_sync_end_date": "2026-06-30",
                        }
                    }
                },
            )
        )
    )

    assert reader.kwargs["initial_lookback_hours"] is None
    assert datetime.fromtimestamp(reader.kwargs["initial_start_time"]).date().isoformat() == "2026-06-01"
    assert datetime.fromtimestamp(reader.kwargs["initial_end_time"]).date().isoformat() == "2026-07-01"
    assert result.next_cursor == "2"
    assert result.stats["has_more"] is True


def test_chrome_history_clear_uses_the_plugin_owned_temp_directory(
    tmp_path: Path,
) -> None:
    from magi_plugin_sdk import UserContentClearContext, UserContentClearRequest

    sensor_cls = _load_sensor_class()
    reader = _ClearableReader()
    sensor = sensor_cls(reader=reader)

    asyncio.run(
        sensor.clear_user_content(
            UserContentClearContext(
                request=UserContentClearRequest(clear_generation=3),
                runtime_paths=_PluginRuntimePaths(tmp_path),
                plugin_id="chrome-history",
                sensor_id="timeline.chrome_history",
                plugin_settings={},
            )
        )
    )

    assert reader.cleared_root == (
        tmp_path
        / "chrome-history"
        / "temporary-database-copies"
        / "browser-history"
    )
