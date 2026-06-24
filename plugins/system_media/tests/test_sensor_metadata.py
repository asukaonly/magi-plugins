from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path


def _load_sensor_module():
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "system_media_metadata_under_test"
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


def test_system_media_policy_allows_l2_music_extraction() -> None:
    mod = _load_sensor_module()
    sensor = mod.SystemMediaTimelineSensor()

    assert sensor.memory_policy.cognition_eligible is True


def test_system_media_output_includes_music_source_facets() -> None:
    mod = _load_sensor_module()
    sensor = mod.SystemMediaTimelineSensor()

    output = asyncio.run(
        sensor.build_output(
            {
                "started_at": "2026-05-17T03:00:00+00:00",
                "ended_at": "2026-05-17T03:03:00+00:00",
                "title": "Song A",
                "artist": "Artist A",
                "album": "Album A",
                "duration_seconds": 180,
                "app_name": "Music",
                "app_id": "com.apple.Music",
            }
        )
    )

    facets = output.domain_payload["source_facets"]
    assert {"name": "music.track", "text": "Song A"} in facets
    assert {"name": "music.artist", "text": "Artist A"} in facets
    assert {"name": "music.album", "text": "Album A"} in facets
    assert {"name": "music.app", "text": "Music"} in facets
    assert {"name": "music.play_count", "numeric": 1} in facets
    assert {"name": "music.play_duration_sec", "numeric": 180} in facets


def test_extract_metadata_emits_listened_fact_hint() -> None:
    mod = _load_sensor_module()
    sensor = mod.SystemMediaTimelineSensor()

    meta = asyncio.run(
        sensor.extract_metadata(
            {
                "started_at": "2026-05-17T03:00:00+00:00",
                "title": "Song A",
                "artist": "Artist A",
                "album": "Album A",
                "duration_seconds": 180,
                "app_name": "Music",
            }
        )
    )

    assert meta.entities == [
        {
            "mention_text": "Song A",
            "entity_type": "media",
            "canonical_name_hint": "Song A",
        },
        {
            "mention_text": "Artist A",
            "entity_type": "person",
            "canonical_name_hint": "Artist A",
        },
        {
            "mention_text": "Album A",
            "entity_type": "media",
            "canonical_name_hint": "Album A",
        },
    ]
    assert meta.fact_hints == [
        {
            "subject_ref": "user:self",
            "subject_type": "user",
            "predicate": "LISTENED",
            "object_ref": "media:Song A",
            "object_type": "media",
            "fact_kind": "interaction_evidence",
            "origin_mode": "source_structured",
            "confidence": 0.9,
            "observed_at": 1778986800.0,
            "attributes": {
                "artist": "Artist A",
                "album": "Album A",
                "app_name": "Music",
                "duration_seconds": 180,
            },
        }
    ]
