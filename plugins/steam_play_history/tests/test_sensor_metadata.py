"""Steam play history L2 metadata behavior."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path


def _load_module(name: str):
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "steam_play_history_under_test"
    package_spec = importlib.util.spec_from_file_location(
        package_name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    package = importlib.util.module_from_spec(package_spec)
    sys.modules[package_name] = package
    package_spec.loader.exec_module(package)
    module_spec = importlib.util.spec_from_file_location(
        f"{package_name}.{name}",
        plugin_dir / f"{name}.py",
    )
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    return module


def test_extract_metadata_emits_media_view_fact_hint_without_llm() -> None:
    sensor_mod = _load_module("sensor")
    sensor = sensor_mod.SteamPlayHistoryTimelineSensor()
    item = {
        "event_kind": "play_session",
        "appid": "1145360",
        "game_name": "Hades",
        "occurred_at": 1_750_000_000.0,
        "duration_seconds": 3600,
        "playtime_forever_minutes_after": 720,
    }

    metadata = asyncio.run(sensor.extract_metadata(item))

    assert sensor.memory_policy.cognition_eligible is True
    assert sensor.memory_policy.allow_llm_extraction is False
    assert metadata.entities == [
        {
            "mention_text": "Hades",
            "entity_type": "media",
            "canonical_name_hint": "Hades",
        }
    ]
    assert metadata.fact_hints == [
        {
            "subject_ref": "user:self",
            "subject_type": "user",
            "predicate": "VIEWED",
            "object_ref": "media:Hades",
            "object_type": "media",
            "fact_kind": "interaction_evidence",
            "origin_mode": "source_structured",
            "confidence": 0.85,
            "observed_at": 1_750_000_000.0,
            "attributes": {
                "provider": "steam",
                "appid": "1145360",
                "event_kind": "play_session",
                "duration_seconds": 3600,
                "playtime_forever_minutes": 720,
            },
        }
    ]
    assert metadata.relation_candidates == []
