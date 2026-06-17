from __future__ import annotations

import asyncio
import sys
from pathlib import Path


PLUGINS_ROOT = str(Path(__file__).resolve().parents[2])
if PLUGINS_ROOT not in sys.path:
    sys.path.insert(0, PLUGINS_ROOT)

from netease_music.sensor import NeteaseMusicTimelineSensor


def test_extract_metadata_emits_listened_as_fact_hint() -> None:
    sensor = NeteaseMusicTimelineSensor()
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
