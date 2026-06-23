from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path


def _load_sensor_module():
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "screen_time_metadata_under_test"
    spec = importlib.util.spec_from_file_location(
        package_name,
        plugin_dir / "__init__.py",
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


def test_screen_time_policy_allows_l2_graph_extraction() -> None:
    mod = _load_sensor_module()
    sensor = mod.ScreenTimeTimelineSensor()

    assert sensor.memory_policy.cognition_eligible is True


def test_extract_metadata_emits_app_usage_fact_hint() -> None:
    mod = _load_sensor_module()
    sensor = mod.ScreenTimeTimelineSensor()

    meta = asyncio.run(
        sensor.extract_metadata(
            {
                "bucket_start": "2026-05-17T03:00:00+00:00",
                "bucket_end": "2026-05-17T04:00:00+00:00",
                "canonical_id": "wuthering_waves",
                "display_name": "Wuthering Waves",
                "category": "gaming",
                "duration_seconds": 1800,
                "session_count": 3,
            }
        )
    )

    assert meta.entities == [
        {
            "mention_text": "Wuthering Waves",
            "entity_type": "software",
            "canonical_name_hint": "wuthering_waves",
        }
    ]
    assert meta.fact_hints == [
        {
            "subject_ref": "user:self",
            "subject_type": "user",
            "predicate": "USES",
            "object_ref": "software:wuthering_waves",
            "object_type": "software",
            "fact_kind": "interaction_evidence",
            "origin_mode": "source_structured",
            "confidence": 0.75,
            "observed_at": 1778990399.0,
            "attributes": {
                "display_name": "Wuthering Waves",
                "category": "gaming",
                "duration_seconds": 1800,
                "session_count": 3,
            },
        }
    ]
