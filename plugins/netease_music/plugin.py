"""NetEase Cloud Music timeline plugin."""
from __future__ import annotations

import sys

from magi_plugin_sdk import ExtensionFieldOption, ExtensionFieldSpec, Plugin, SensorSpec
from .reader import DEFAULT_DB_PATH
from .sensor import NeteaseMusicTimelineSensor
from .summary_features import build_netease_temporal_summary_features

DEFAULT_SETTINGS = {
    "enabled": False,
    "sync_mode": "manual",
    "sync_interval_minutes": 30,
    "min_play_duration": 20,
    "db_path": DEFAULT_DB_PATH,
    "default_retention_mode": "analyze_only",
    "storage_mode": "managed",
    "initial_sync_policy": "from_now",
}


def _budget_int(budget: object | None, key: str, default: int) -> int:
    if budget is None:
        return int(default)
    if isinstance(budget, dict):
        raw = budget.get(key, default)
    else:
        raw = getattr(budget, key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


def _normalize_sync_mode(value: object | None) -> str:
    mode = str(value or DEFAULT_SETTINGS["sync_mode"]).strip().lower()
    if mode == "watch":
        return "interval"
    if mode in {"manual", "interval"}:
        return mode
    return str(DEFAULT_SETTINGS["sync_mode"])


def _fields(prefix: str) -> list[ExtensionFieldSpec]:
    return [
        ExtensionFieldSpec(
            key=f"{prefix}.enabled",
            type="switch",
            label="Enabled",
            description="Whether NetEase Music sync is active.",
            default=False,
            section="general",
            surface="timeline",
            order=10,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sync_mode",
            type="select",
            label="Sync Mode",
            description="How NetEase Music history should be synchronized.",
            default="manual",
            options=[
                ExtensionFieldOption(label="Manual", value="manual"),
                ExtensionFieldOption(label="Interval", value="interval"),
            ],
            section="general",
            surface="timeline",
            order=20,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sync_interval_minutes",
            type="number",
            label="Sync Interval (minutes)",
            description="Polling interval used for interval-based sync.",
            default=30,
            section="general",
            surface="timeline",
            order=30,
            depends_on_key=f"{prefix}.sync_mode",
            depends_on_values=["interval"],
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.min_play_duration",
            type="number",
            label="Minimum Play Duration (seconds)",
            description="Minimum track play duration to include in timeline (seconds).",
            default=20,
            min=1,
            section="general",
            surface="timeline",
            order=40,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.initial_sync_policy",
            type="select",
            label="Initial Sync Policy",
            description="How the first sync should seed the timeline before running.",
            default="from_now",
            options=[
                ExtensionFieldOption(label="Backfill all history", value="full"),
                ExtensionFieldOption(label="Sync recent days", value="lookback_days"),
                ExtensionFieldOption(label="Only new records from now on", value="from_now"),
            ],
            section="activation",
            surface="timeline",
            order=80,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.tag_strategy",
            type="select",
            label="Genre Tag Source",
            description=(
                "How to extract genre/style tags for listened tracks. "
                "'Built-in' reads local alias data (limited coverage). "
                "'Last.fm' queries the Last.fm API (requires an API key)."
            ),
            default="off",
            options=[
                ExtensionFieldOption(label="Off", value="off"),
                ExtensionFieldOption(label="Built-in (local alias data)", value="builtin"),
                ExtensionFieldOption(label="Last.fm", value="lastfm"),
            ],
            section="analysis",
            surface="timeline",
            order=75,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.lastfm_api_key",
            type="input",
            label="Last.fm API Key",
            description=(
                "Required when Genre Tag Source is set to Last.fm. "
                "Apply for a free key at last.fm/api/account/create"
            ),
            default="",
            section="analysis",
            surface="timeline",
            order=76,
            depends_on_key=f"{prefix}.tag_strategy",
            depends_on_values=["lastfm"],
        ),
    ]


class NeteaseMusicPlugin(Plugin):
    """Registers the NetEase Music timeline source."""

    def build_temporal_summary_features(
        self,
        *,
        source_type: str,
        events: list[dict[str, object]],
        summary_category: str,
        period_start: float,
        period_end: float,
        budget: object | None = None,
    ) -> dict[str, object]:
        """Expose genre-oriented summary hints for L3 temporal summaries."""

        _ = (summary_category, period_start, period_end)
        if source_type != "netease_music":
            return {}

        features = build_netease_temporal_summary_features(events)
        top_tags = features.get("top_tags") or []
        top_tracks = features.get("top_tracks") or []
        top_artists = features.get("top_artists") or []
        top_albums = features.get("top_albums") or []
        if not any((top_tags, top_tracks, top_artists, top_albums)):
            return {}

        formatted_tags = ", ".join(
            f"{item['tag']} ({item['count']})"
            for item in top_tags
            if isinstance(item, dict) and item.get("tag")
        )
        formatted_tracks = ", ".join(
            f"{item['track']} ({item['count']})"
            for item in top_tracks
            if isinstance(item, dict) and item.get("track")
        )
        formatted_artists = ", ".join(
            f"{item['artist']} ({item['count']})"
            for item in top_artists
            if isinstance(item, dict) and item.get("artist")
        )
        formatted_albums = ", ".join(
            f"{item['album']} ({item['count']})"
            for item in top_albums
            if isinstance(item, dict) and item.get("album")
        )

        tagged_event_count = int(features.get("tagged_event_count") or 0)
        covered_event_count = int(features.get("total_event_count") or 0)
        total_event_count = _budget_int(budget, "total_event_count", covered_event_count)
        omitted_event_count = max(0, total_event_count - covered_event_count)
        summary_lines: list[str] = [
            self.t(
                "summary_features.genre_coverage",
                tagged_events=tagged_event_count,
                total_events=covered_event_count,
                fallback=f"Genre tags appeared on {tagged_event_count} of {covered_event_count} covered listening events.",
            ),
        ]
        if formatted_tags:
            summary_lines.append(
                self.t(
                    "summary_features.top_tags",
                    top_tags=formatted_tags,
                    fallback=f"Top genre signals: {formatted_tags}.",
                )
            )
        if formatted_artists:
            summary_lines.append(
                self.t(
                    "summary_features.top_artists",
                    top_artists=formatted_artists,
                    fallback=f"Top artists: {formatted_artists}.",
                )
            )
        if formatted_tracks:
            summary_lines.append(
                self.t(
                    "summary_features.top_tracks",
                    top_tracks=formatted_tracks,
                    fallback=f"Top tracks: {formatted_tracks}.",
                )
            )
        if formatted_albums:
            summary_lines.append(
                self.t(
                    "summary_features.top_albums",
                    top_albums=formatted_albums,
                    fallback=f"Top albums: {formatted_albums}.",
                )
            )
        if omitted_event_count > 0:
            summary_lines.append(
                f"Music feature coverage used {covered_event_count} representative events; {omitted_event_count} additional events were compacted."
            )

        return {
            **features,
            "total_event_count": total_event_count,
            "covered_event_count": covered_event_count,
            "omitted_event_count": omitted_event_count,
            "coverage_ratio": (covered_event_count / total_event_count) if total_event_count else None,
            "top_entities": [
                *[
                    {"type": "artist", "name": item["artist"], "count": item["count"]}
                    for item in top_artists
                    if isinstance(item, dict) and item.get("artist")
                ],
                *[
                    {"type": "track", "name": item["track"], "count": item["count"]}
                    for item in top_tracks
                    if isinstance(item, dict) and item.get("track")
                ],
                *[
                    {"type": "album", "name": item["album"], "count": item["count"]}
                    for item in top_albums
                    if isinstance(item, dict) and item.get("album")
                ],
            ],
            "summary_lines": [line for line in summary_lines if str(line).strip()],
        }

    def get_sensors(self) -> list[tuple[str, object, SensorSpec]]:
        if sys.platform not in ("darwin", "win32"):
            return []

        settings = {}
        sensors_settings = self.settings.get("sensors", {})
        if isinstance(sensors_settings, dict):
            settings = dict(sensors_settings.get("netease_music", {}))
        resolved_sync_mode = _normalize_sync_mode(settings.get("sync_mode"))

        sensor = NeteaseMusicTimelineSensor(
            min_play_duration=int(settings.get("min_play_duration") or DEFAULT_SETTINGS["min_play_duration"]),
            source_path=str(settings.get("db_path") or DEFAULT_SETTINGS["db_path"]),
            retention_mode=str(settings.get("default_retention_mode") or DEFAULT_SETTINGS["default_retention_mode"]),
            tag_strategy=str(settings.get("tag_strategy") or "off"),
            lastfm_api_key=str(settings.get("lastfm_api_key") or ""),
        )

        return [
            (
                "timeline.netease_music",
                sensor,
                SensorSpec(
                    sensor_id="timeline.netease_music",
                    display_name="NetEase Cloud Music",
                    description="Local NetEase Cloud Music play history ingestion for the timeline.",
                    domain="timeline",
                    surface="timeline",
                    sync_mode=resolved_sync_mode,
                    polling_mode=getattr(sensor, "polling_mode", "interval"),
                    fields=_fields("sensors.netease_music"),
                    metadata={
                        "source_type": "netease_music",
                        "default_settings": dict(DEFAULT_SETTINGS),
                    },
                ),
            )
        ]