"""Cross-platform system media playback timeline plugin."""
from __future__ import annotations

from collections import Counter
import sys
from typing import Any

from magi_plugin_sdk import ExtensionFieldOption, ExtensionFieldSpec, Plugin, SensorSpec

from .sensor import SystemMediaTimelineSensor
from .state import MediaSessionStateStore

DEFAULT_SETTINGS = {
    "enabled": False,
    "sync_interval_minutes": 1,
    "min_session_seconds": 30,
    "pause_timeout_seconds": 300,
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


def _event_provenance(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata_json")
    if not isinstance(metadata, dict):
        return {}
    timeline = metadata.get("timeline")
    if not isinstance(timeline, dict):
        return {}
    provenance = timeline.get("provenance")
    return provenance if isinstance(provenance, dict) else {}


def _int_value(mapping: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(mapping.get(key) or default)
    except (TypeError, ValueError):
        return int(default)


def _format_minutes(seconds: int) -> str:
    minutes = max(0, round(seconds / 60))
    if minutes >= 60:
        hours = minutes // 60
        remainder = minutes % 60
        return f"{hours}h {remainder}m" if remainder else f"{hours}h"
    return f"{minutes}m"


def _fields(prefix: str) -> list[ExtensionFieldSpec]:
    """Settings fields for the system media plugin."""
    return [
        ExtensionFieldSpec(
            key=f"{prefix}.enabled",
            type="switch",
            label="Enable Media Recording",
            description="Automatically detect and record music and videos playing on your device.",
            default=False,
            section="general",
            surface="timeline",
            order=10,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sync_interval_minutes",
            type="select",
            label="Recording Frequency",
            description="How often completed playback records are saved to memory.",
            default=1,
            options=[
                ExtensionFieldOption(label="Every 1 minute", value="1"),
                ExtensionFieldOption(label="Every 5 minutes", value="5"),
                ExtensionFieldOption(label="Every 15 minutes", value="15"),
            ],
            section="sync",
            surface="timeline",
            order=20,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.min_session_seconds",
            type="number",
            label="Minimum Play Duration (seconds)",
            description="Tracks played shorter than this are ignored (skipped songs won't be recorded).",
            default=30,
            min=5,
            section="sync",
            surface="timeline",
            order=30,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.pause_timeout_seconds",
            type="number",
            label="Pause Before Ending Record (seconds)",
            description="When paused for longer than this, the current record is saved. 300 = 5 minutes.",
            default=300,
            min=30,
            section="sync",
            surface="timeline",
            order=40,
        ),
    ]


class SystemMediaPlugin(Plugin):
    """Registers the system-media timeline sensor."""

    def build_temporal_summary_features(
        self,
        *,
        source_type: str,
        events: list[dict[str, Any]],
        summary_category: str,
        period_start: float,
        period_end: float,
        budget: object | None = None,
    ) -> dict[str, object] | None:
        """Aggregate playback-session features for L3 summaries."""
        _ = summary_category, period_start, period_end
        if source_type != "system_media" or not events:
            return None

        artist_counter: Counter[str] = Counter()
        app_counter: Counter[str] = Counter()
        track_counter: Counter[str] = Counter()
        total_duration_seconds = 0
        representative_event_ids: list[str] = []

        for event in events:
            provenance = _event_provenance(event)
            artist = str(provenance.get("artist") or "").strip()
            if artist:
                artist_counter[artist] += 1
            app_name = str(provenance.get("app_name") or provenance.get("app_id") or "Media App").strip()
            app_counter[app_name] += 1
            title = str(provenance.get("title") or "").strip()
            if title:
                label = f"{title} - {artist}" if artist else title
                track_counter[label] += 1
            total_duration_seconds += _int_value(provenance, "duration_seconds")
            event_id = str(event.get("event_id") or "").strip()
            if event_id and len(representative_event_ids) < 8:
                representative_event_ids.append(event_id)

        covered_event_count = len(events)
        total_event_count = _budget_int(budget, "total_event_count", covered_event_count)
        omitted_event_count = max(0, total_event_count - covered_event_count)
        top_artists = [
            {"artist": artist, "session_count": count}
            for artist, count in artist_counter.most_common(6)
        ]
        top_apps = [
            {"app": app, "session_count": count}
            for app, count in app_counter.most_common(4)
        ]
        top_tracks = [
            {"track": track, "session_count": count}
            for track, count in track_counter.most_common(6)
        ]

        summary_lines = [
            f"Media feature coverage used {covered_event_count} playback sessions totaling {_format_minutes(total_duration_seconds)}."
        ]
        if top_artists:
            joined = ", ".join(f"{item['artist']} ({item['session_count']})" for item in top_artists[:4])
            summary_lines.append(f"Top media artists: {joined}.")
        if top_apps:
            joined = ", ".join(f"{item['app']} ({item['session_count']})" for item in top_apps[:3])
            summary_lines.append(f"Playback apps represented: {joined}.")
        if omitted_event_count > 0:
            summary_lines.append(
                f"Media feature coverage used {covered_event_count} representative sessions; {omitted_event_count} additional sessions were compacted."
            )

        return {
            "feature_type": "system_media",
            "session_count": covered_event_count,
            "total_event_count": total_event_count,
            "covered_event_count": covered_event_count,
            "omitted_event_count": omitted_event_count,
            "coverage_ratio": (covered_event_count / total_event_count) if total_event_count else None,
            "total_duration_seconds": total_duration_seconds,
            "top_entities": [{"type": "artist", **item} for item in top_artists],
            "top_apps": top_apps,
            "top_tracks": top_tracks,
            "representative_event_ids": representative_event_ids,
            "summary_lines": summary_lines,
        }

    def get_sensors(self) -> list[tuple[str, object, SensorSpec]]:
        if sys.platform not in ("win32", "darwin"):
            return []

        settings: dict = {}
        sensors_settings = self.settings.get("sensors", {})
        if isinstance(sensors_settings, dict):
            settings = dict(sensors_settings.get("system_media", {}))

        min_session_s = int(settings.get("min_session_seconds", DEFAULT_SETTINGS["min_session_seconds"]))
        pause_timeout_s = int(settings.get("pause_timeout_seconds", DEFAULT_SETTINGS["pause_timeout_seconds"]))
        sync_interval = int(settings.get("sync_interval_minutes", DEFAULT_SETTINGS["sync_interval_minutes"]))

        state_store = MediaSessionStateStore(
            pause_timeout_s=pause_timeout_s,
            min_session_s=min_session_s,
        )
        sensor = SystemMediaTimelineSensor(state_store=state_store)

        return [
            (
                "timeline.system_media",
                sensor,
                SensorSpec(
                    sensor_id="timeline.system_media",
                    display_name="System Media",
                    description="Cross-platform media playback tracking via OS transport controls.",
                    domain="timeline",
                    surface="timeline",
                    sync_mode="interval",
                    polling_mode="interval",
                    fields=_fields("sensors.system_media"),
                    metadata={
                        "source_type": "system_media",
                        "default_settings": dict(DEFAULT_SETTINGS),
                        "sync_interval_minutes": sync_interval,
                    },
                ),
            )
        ]
