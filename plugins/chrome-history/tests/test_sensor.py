from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


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
