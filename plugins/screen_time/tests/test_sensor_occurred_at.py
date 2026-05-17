"""Regression tests for screen-time bucket → event timestamp mapping."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_sensor_module() -> ModuleType:
    """Load ``sensor.py`` without going through the plugin package import.

    The plugin package depends on a private state store and watcher whose
    full import chain is platform-dependent. Loading the sensor module by
    file path keeps this test self-contained and OS-agnostic.
    """
    plugin_dir = Path(__file__).resolve().parents[1]
    # ``sensor.py`` does ``from ._watcher import ...`` etc., so we have to
    # register the plugin directory as a package first.
    package_name = "screen_time_under_test"
    package_init = plugin_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        package_name,
        package_init,
        submodule_search_locations=[str(plugin_dir)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)

    sensor_spec = importlib.util.spec_from_file_location(
        f"{package_name}.sensor",
        plugin_dir / "sensor.py",
    )
    assert sensor_spec is not None and sensor_spec.loader is not None
    module = importlib.util.module_from_spec(sensor_spec)
    sys.modules[sensor_spec.name] = module
    sensor_spec.loader.exec_module(module)
    return module


def test_build_output_anchors_occurred_at_to_bucket_end() -> None:
    """A bucket covering 11:00–12:00 must emit an event timestamped just
    inside the bucket so "last hour ending at 12:02" queries cover it.
    """
    sensor_module = _load_sensor_module()
    sensor = sensor_module.ScreenTimeTimelineSensor()

    item = {
        "bucket_start": "2026-05-17T03:00:00+00:00",  # 11:00 CST
        "bucket_end": "2026-05-17T04:00:00+00:00",    # 12:00 CST
        "bundle_id": "win32:client-win64-shipping.exe",
        "app_name": "Wuthering Waves",
        "canonical_id": "win32:client-win64-shipping.exe",
        "display_name": "Wuthering Waves",
        "platform": "win32",
        "duration_seconds": 2280,
        "session_count": 17,
    }

    output = asyncio.run(sensor.build_output(item))

    bucket_end_ts = 1778990400.0  # 2026-05-17T04:00:00Z
    bucket_start_ts = 1778986800.0  # 2026-05-17T03:00:00Z

    # The event must land inside the bucket interval but close to the end
    # so an immediately-following "past 1h" query still includes it.
    assert output.occurred_at == bucket_end_ts - 1.0
    assert output.occurred_at > bucket_start_ts
    assert output.occurred_at < bucket_end_ts


def test_build_output_window_just_after_bucket_end_includes_event() -> None:
    """Simulate the exact bug: ask "last hour" at 12:02 → window [11:02,12:02].
    The bucket [11:00, 12:00] event must fall inside that window.
    """
    sensor_module = _load_sensor_module()
    sensor = sensor_module.ScreenTimeTimelineSensor()

    item = {
        "bucket_start": "2026-05-17T03:00:00+00:00",
        "bucket_end": "2026-05-17T04:00:00+00:00",
        "bundle_id": "app",
        "app_name": "App",
        "canonical_id": "app",
        "display_name": "App",
        "platform": "win32",
        "duration_seconds": 1800,
        "session_count": 5,
    }

    output = asyncio.run(sensor.build_output(item))

    user_question_ts = 1778990537.213          # 12:02:17 CST
    one_hour_window_start = user_question_ts - 3600  # 11:02:17 CST

    assert one_hour_window_start <= output.occurred_at <= user_question_ts
