from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_sensor_module() -> ModuleType:
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "browser_history_core_sensor_under_test"
    if package_name not in sys.modules:
        package_spec = importlib.util.spec_from_file_location(
            package_name,
            plugin_dir / "__init__.py",
            submodule_search_locations=[str(plugin_dir)],
        )
        assert package_spec is not None and package_spec.loader is not None
        package = importlib.util.module_from_spec(package_spec)
        sys.modules[package_name] = package
        package_spec.loader.exec_module(package)

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.sensor_base",
        plugin_dir / "sensor_base.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _RuntimePaths:
    def plugin_cache_dir(self, plugin_id: str) -> Path:
        return Path("/tmp") / plugin_id


class _Reader:
    def read_visits(self, *, limit: int, **kwargs):
        return [
            {
                "visit_id": str(idx),
                "last_visit_id": str(idx),
                "visit_time": 1_710_000_000.0 + idx,
                "canonical_url": f"https://example.com/{idx}",
                "url": f"https://example.com/{idx}",
                "domain": "example.com",
                "title": f"Page {idx}",
                "merged_visit_count": 1,
            }
            for idx in range(1, limit + 1)
        ]

    def get_latest_visit_id(self, **kwargs) -> int:
        return 100


def test_browser_history_marks_has_more_when_limit_is_full() -> None:
    mod = _load_sensor_module()
    sensor = mod.BaseBrowserHistoryTimelineSensor(reader=_Reader())

    from magi_plugin_sdk.sensors import SensorSyncContext

    result = asyncio.run(
        sensor.collect_items(
            SensorSyncContext(
                source_type="browser_history",
                manual=True,
                last_cursor="0",
                last_success_at=None,
                limit=3,
                runtime_paths=_RuntimePaths(),
                plugin_settings={"sensors": {"browser_history": {}}},
            )
        )
    )

    assert result.stats["has_more"] is True
    assert result.next_cursor == "3"
