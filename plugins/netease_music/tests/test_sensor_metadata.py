from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

PLUGINS_ROOT = str(Path(__file__).resolve().parents[2])
if PLUGINS_ROOT not in sys.path:
    sys.path.insert(0, PLUGINS_ROOT)


def _sensor_cls():
    return importlib.import_module("netease_music.sensor").NeteaseMusicTimelineSensor


def test_build_output_includes_music_source_facets() -> None:
    sensor = _sensor_cls()()

    output = asyncio.run(
        sensor.build_output(
            {
                "id": "play-1",
                "track_id": "12345",
                "track_name": "Song A",
                "artist_id": "678",
                "artist_name": "Artist A",
                "album_id": "999",
                "album_name": "Album A",
                "play_duration_sec": 180,
                "track_alias": ["Alias A"],
                "update_time": 1_710_000_000.0,
            }
        )
    )

    facets = output.domain_payload["source_facets"]
    assert {"name": "music.track", "text": "Song A"} in facets
    assert {"name": "music.track_alias", "text": "Alias A"} in facets
    assert {"name": "music.artist", "text": "Artist A"} in facets
    assert {"name": "music.album", "text": "Album A"} in facets
    assert {"name": "music.track_id", "text": "12345"} in facets
    assert {"name": "music.play_count", "numeric": 1} in facets
    assert {"name": "music.play_duration_sec", "numeric": 180} in facets


def test_extract_metadata_emits_listened_as_fact_hint() -> None:
    sensor = _sensor_cls()()
    meta = asyncio.run(
        sensor.extract_metadata(
            {
                "track_name": "Song A",
                "artist_name": "Artist A",
                "album_name": "Album A",
            }
        )
    )

    assert meta.relation_candidates == []
    assert meta.fact_hints == [
        {
            "subject_ref": "user:self",
            "subject_type": "user",
            "predicate": "LISTENED",
            "object_ref": "media:Song A",
            "object_type": "media",
            "fact_kind": "interaction_evidence",
            "origin_mode": "source_structured",
            "confidence": 1.0,
        }
    ]
