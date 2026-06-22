from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_sensor_module() -> ModuleType:
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "photo_library_sensor_under_test"
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


def test_extract_metadata_emits_session_facts_as_fact_hints() -> None:
    mod = _load_sensor_module()
    sensor = mod.PhotoLibraryTimelineSensor()
    meta = asyncio.run(
        sensor.extract_metadata(
            {
                "device_slug": "iphone15",
                "device_name": "My iPhone",
                "latitude": 1.0,
                "longitude": 2.0,
                "location_name": "Riverside Park",
                "photo_count": 4,
                "first_capture_ts": 1710000000.0,
            }
        )
    )

    relations = {(rel["predicate"], rel["object_id"], rel["object_type"]) for rel in meta.relation_candidates}
    assert relations == {
        ("OWNS", "device:iphone15", "device"),
        ("VISITED", "location:Riverside Park", "place"),
    }
    facts = {(fact["predicate"], fact["object_ref"], fact["object_type"]) for fact in meta.fact_hints}
    assert facts == {
        ("OWNS", "hardware:iphone15", "hardware"),
        ("VISITED", "place:Riverside Park", "place"),
    }
    for fact in meta.fact_hints:
        assert fact["subject_ref"] == "user:self"
        assert fact["subject_type"] == "user"
        assert fact["origin_mode"] == "source_structured"


def test_extract_metadata_adds_apple_place_details_to_retrieval_terms() -> None:
    mod = _load_sensor_module()
    sensor = mod.PhotoLibraryTimelineSensor()

    meta = asyncio.run(
        sensor.extract_metadata(
            {
                "device_slug": "iphone16",
                "device_name": "Apple iPhone 16 Pro Max",
                "latitude": 35.661545,
                "longitude": 139.74629166666668,
                "location_name": "Honshu, Minato, Tokyo, Japan",
                "location_source": "apple_photos",
                "apple_photos_place_name": "Honshu, Minato, Tokyo, Japan",
                "apple_photos_place_address": (
                    "大手第二ビル, 23-15, Toranomon 3-Chōme, "
                    "Minato, Tokyo, Japan 105-0001"
                ),
                "photo_count": 1,
                "first_capture_ts": 1_735_372_123.182,
            }
        )
    )

    assert meta.tags == [
        "photo_library",
        "session",
        "geo",
        "Honshu, Minato, Tokyo, Japan",
        "大手第二ビル, 23-15, Toranomon 3-Chōme, Minato, Tokyo, Japan 105-0001",
        "东京",
        "日本",
    ]
