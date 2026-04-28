from __future__ import annotations

import json
from collections import Counter
from typing import Any

_GENERIC_TIMELINE_TAGS = {"netease_music", "music", "listening", "liked"}


def build_netease_temporal_summary_features(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate genre-oriented summary features from L1 NetEase events."""

    total_event_count = 0
    tagged_event_count = 0
    liked_event_count = 0
    tag_counter: Counter[str] = Counter()
    track_counter: Counter[str] = Counter()
    artist_counter: Counter[str] = Counter()
    album_counter: Counter[str] = Counter()

    for event in events:
        if not isinstance(event, dict):
            continue
        total_event_count += 1
        metadata = _coerce_mapping(event.get("metadata_json"))
        timeline = _coerce_mapping(metadata.get("timeline"))
        provenance = _coerce_mapping(timeline.get("provenance"))

        track_name = _normalized_value(provenance.get("track_name"))
        artist_name = _normalized_value(provenance.get("artist_name"))
        album_name = _normalized_value(provenance.get("album_name"))

        if track_name:
            track_counter[track_name] += 1
        if artist_name:
            artist_counter[artist_name] += 1
        if album_name:
            album_counter[album_name] += 1

        if bool(provenance.get("is_liked")):
            liked_event_count += 1

        event_tags = _extract_event_tags(metadata)
        if not event_tags:
            continue
        tagged_event_count += 1
        for tag in event_tags:
            tag_counter[tag] += 1

    top_tags = [
        {"tag": tag, "count": count}
        for tag, count in tag_counter.most_common(5)
    ]
    top_tracks = [
        {"track": track, "count": count}
        for track, count in track_counter.most_common(5)
    ]
    top_artists = [
        {"artist": artist, "count": count}
        for artist, count in artist_counter.most_common(5)
    ]
    top_albums = [
        {"album": album, "count": count}
        for album, count in album_counter.most_common(5)
    ]
    coverage_ratio = (tagged_event_count / total_event_count) if total_event_count else 0.0
    return {
        "total_event_count": total_event_count,
        "tagged_event_count": tagged_event_count,
        "liked_event_count": liked_event_count,
        "coverage_ratio": round(coverage_ratio, 4),
        "top_tags": top_tags,
        "top_tracks": top_tracks,
        "top_artists": top_artists,
        "top_albums": top_albums,
    }


def _extract_event_tags(metadata: dict[str, Any]) -> list[str]:
    projection = _coerce_mapping(metadata.get("projection"))
    retrieval_terms = _normalize_terms(projection.get("retrieval_terms"))
    if retrieval_terms:
        return retrieval_terms

    timeline = _coerce_mapping(metadata.get("timeline"))
    return [
        tag
        for tag in _normalize_terms(timeline.get("tags"))
        if tag.casefold() not in _GENERIC_TIMELINE_TAGS
    ]


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _normalize_terms(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        term = " ".join(str(item or "").strip().split())
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(term)
    return normalized


def _normalized_value(value: Any) -> str:
    return " ".join(str(value or "").strip().split())