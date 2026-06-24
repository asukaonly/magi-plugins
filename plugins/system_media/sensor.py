"""Timeline sensor for cross-platform media playback sessions."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from magi_plugin_sdk.sensors import (
    ContentBlock,
    SensorBase,
    SensorMemoryPolicy,
    SensorOutput,
    SensorOutputMetadata,
    SensorSyncContext,
    SensorSyncResult,
)

from .reader import get_current_media
from .state import MediaSessionStateStore


class SystemMediaTimelineSensor(SensorBase):
    """Poll OS media state and flush completed listening sessions."""

    sensor_id = "timeline.system_media"
    display_name = "System Media"
    source_type = "system_media"
    memory_event_type = "MEDIA_LISTEN_SESSION"
    polling_mode = "interval"
    default_interval = 15
    update_key_fields = ("started_at", "app_id", "title")
    supports_pull_sync = True
    supports_state_flush = True

    memory_policy = SensorMemoryPolicy(
        memory_domain="external_activity",
        ingest_target="l1_only",
        cognition_eligible=True,
        tom_depth="none",
        retention_class="compressible",
        importance_bias=0.4,
        author_type="external",
        content_type="observation",
    )

    def __init__(self, *, state_store: MediaSessionStateStore | None = None) -> None:
        super().__init__()
        self._state_store = state_store or MediaSessionStateStore()

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def source_item_identity(self, item: dict[str, Any]) -> str:
        return (
            f"media:{item.get('started_at', '')}:{item.get('app_id', '')}:{item.get('title', '')}"
        )

    def source_item_version_fingerprint(self, item: dict[str, Any]) -> str:
        parts = [
            str(item.get("started_at", "")),
            str(item.get("app_id", "")),
            str(item.get("title", "")),
            str(item.get("duration_seconds", 0)),
        ]
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()

    async def extract_metadata(self, item: dict[str, Any]) -> SensorOutputMetadata:
        title = str(item.get("title") or "").strip()
        artist = str(item.get("artist") or "").strip()
        album = str(item.get("album") or "").strip()
        if not title:
            return SensorOutputMetadata()

        entities: list[dict[str, Any]] = [
            {
                "mention_text": title,
                "entity_type": "media",
                "canonical_name_hint": title,
            }
        ]
        if artist:
            entities.append(
                {
                    "mention_text": artist,
                    "entity_type": "person",
                    "canonical_name_hint": artist,
                }
            )
        if album:
            entities.append(
                {
                    "mention_text": album,
                    "entity_type": "media",
                    "canonical_name_hint": album,
                }
            )

        attributes: dict[str, Any] = {}
        if artist:
            attributes["artist"] = artist
        if album:
            attributes["album"] = album
        app_name = str(item.get("app_name") or item.get("app_id") or "").strip()
        if app_name:
            attributes["app_name"] = app_name
        attributes["duration_seconds"] = int(item.get("duration_seconds") or 0)

        fact_hint: dict[str, Any] = {
            "subject_ref": "user:self",
            "subject_type": "user",
            "predicate": "LISTENED",
            "object_ref": f"media:{title}",
            "object_type": "media",
            "fact_kind": "interaction_evidence",
            "origin_mode": "source_structured",
            "confidence": 0.9,
            "attributes": attributes,
        }
        observed_at = _parse_started_at(item.get("started_at"))
        if observed_at is not None:
            fact_hint["observed_at"] = observed_at

        return SensorOutputMetadata(
            entities=entities,
            tags=["media", "music", "listening"],
            fact_hints=[fact_hint],
            relation_candidates=[],
        )

    async def collect_items(self, context: SensorSyncContext) -> SensorSyncResult:
        now = self._now()

        # 1. Poll current OS media state and feed into state store
        media = await get_current_media()
        await self._state_store.apply_poll(
            runtime_paths=context.runtime_paths,
            media=media,
            now=now,
        )

        # 2. Flush completed sessions
        items = await self._state_store.flush_completed(runtime_paths=context.runtime_paths)
        items.sort(key=lambda i: i.get("started_at", ""), reverse=True)

        return SensorSyncResult(
            items=items,
            next_cursor=str(now.timestamp()),
            watermark_ts=now.timestamp(),
            stats={"count": len(items)},
        )

    async def flush_runtime_state(
        self, *, runtime_paths: Any, plugin_settings: dict[str, Any]
    ) -> dict[str, Any]:
        _ = plugin_settings
        return await self._state_store.flush_in_progress(
            runtime_paths=runtime_paths, now=self._now()
        )

    async def build_output(self, item: dict[str, Any]) -> SensorOutput:
        started_at = datetime.fromisoformat(str(item["started_at"]))
        duration_seconds = int(item.get("duration_seconds", 0))
        title = str(item.get("title", ""))
        artist = str(item.get("artist", ""))
        album = str(item.get("album", ""))
        app_name = str(item.get("app_name", ""))
        app_id = str(item.get("app_id", ""))

        duration_minutes = max(1, round(duration_seconds / 60))

        if artist:
            headline = f"Listened to '{title}' by {artist} for {duration_minutes}m"
            summary = f"Played '{title}' by {artist}"
        else:
            headline = f"Listened to '{title}' for {duration_minutes}m"
            summary = f"Played '{title}'"
        if app_name:
            summary += f" ({app_name})"
        summary += f" for {duration_minutes} minute{'s' if duration_minutes != 1 else ''}."

        blocks = [
            ContentBlock(kind="text", value=f"Track: {title}"),
        ]
        if artist:
            blocks.append(ContentBlock(kind="text", value=f"Artist: {artist}"))
        if album:
            blocks.append(ContentBlock(kind="text", value=f"Album: {album}"))
        blocks.append(ContentBlock(kind="text", value=f"App: {app_name or app_id}"))
        blocks.append(ContentBlock(kind="text", value=f"Duration: {duration_seconds}s"))

        return self._build_output(
            source_item_id=self.source_item_identity(item),
            activity=self._build_activity(
                source=self._build_activity_facet(
                    code=app_id or app_name.lower().replace(" ", "_") or "media_app",
                    i18n_key=f"apps.{app_id or 'media'}",
                    fallback=app_name or app_id or "Media",
                    embedding_fallback=app_name or app_id or "媒体",
                ),
                action=self._build_activity_facet(
                    code="playback",
                    i18n_key="activity.action.playback",
                    fallback="Playback",
                    embedding_fallback="播放",
                ),
            ),
            narration=self._build_narration(title=headline, body=summary),
            occurred_at=started_at.timestamp(),
            content_blocks=blocks,
            tags=["media", "music", "listening"],
            provenance={
                "sensor_id": self.sensor_id,
                "app_name": app_name,
                "app_id": app_id,
                "title": title,
                "artist": artist,
                "album": album,
                "started_at": str(item.get("started_at", "")),
                "ended_at": str(item.get("ended_at", "")),
                "duration_seconds": duration_seconds,
            },
            domain_payload={
                "retention_mode": "analyze_only",
                "title": title,
                "artist": artist,
                "album": album,
                "app_name": app_name,
                "app_id": app_id,
                "started_at": str(item.get("started_at", "")),
                "ended_at": str(item.get("ended_at", "")),
                "duration_seconds": duration_seconds,
                "source_facets": _build_music_source_facets(item),
            },
        )


def _build_music_source_facets(item: dict[str, Any]) -> list[dict[str, Any]]:
    facets: list[dict[str, Any]] = []
    for key, facet_name in (
        ("title", "music.track"),
        ("artist", "music.artist"),
        ("album", "music.album"),
        ("app_name", "music.app"),
        ("app_id", "music.app"),
    ):
        value = str(item.get(key) or "").strip()
        if value:
            facets.append({"name": facet_name, "text": value})
    facets.append({"name": "music.play_count", "numeric": 1})
    facets.append(
        {"name": "music.play_duration_sec", "numeric": int(item.get("duration_seconds") or 0)}
    )
    return facets


def _parse_started_at(raw_value: object) -> float | None:
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(str(raw_value)).timestamp()
    except ValueError:
        return None
