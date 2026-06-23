"""Foreground app usage timeline plugin (cross-platform)."""
from __future__ import annotations

import sys
from collections import Counter
from typing import Any

from magi_plugin_sdk import (
    ExtensionFieldOption,
    ExtensionFieldSpec,
    ExtractionProfileSpec,
    Plugin,
    SensorSpec,
)

from .sensor import ScreenTimeTimelineSensor

DEFAULT_SETTINGS = {
    "enabled": False,
    "sync_interval_minutes": 5,
}

SUPPORTED_PLATFORMS = ("darwin", "win32")


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
    """Define all settings fields for the foreground app usage plugin."""
    return [
        ExtensionFieldSpec(
            key=f"{prefix}.enabled",
            type="switch",
            label="Enable App Usage Sync",
            description=(
                "Tracks the foreground app every second and writes one summary per app "
                "for each clock hour (10:00-11:00, 11:00-12:00, ...). A bucket is only "
                "written to your timeline after the hour it belongs to has fully ended, "
                "so the first entry typically appears 0-59 minutes after you enable this."
            ),
            default=False,
            section="general",
            surface="timeline",
            order=10,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sync_interval_minutes",
            type="select",
            label="Check Interval",
            description=(
                "How often the plugin checks for hourly buckets that have just ended and "
                "flushes them to the timeline. This does NOT change how often new entries "
                "appear (always one per app per clock hour); a smaller value only reduces "
                "the delay between an hour ending and its records becoming visible."
            ),
            default=5,
            options=[
                ExtensionFieldOption(label="Every minute", value="1"),
                ExtensionFieldOption(label="Every 5 minutes", value="5"),
                ExtensionFieldOption(label="Every 15 minutes", value="15"),
                ExtensionFieldOption(label="Every 60 minutes", value="60"),
            ],
            section="sync",
            surface="timeline",
            order=20,
        ),
    ]


class ScreenTimePlugin(Plugin):
    """Registers the cross-platform foreground app usage source."""

    def get_extraction_profiles(self) -> list[ExtractionProfileSpec]:
        return [
            ExtractionProfileSpec(
                profile_id="source.screen_time",
                source_types=["screen_time"],
                allowed_entity_types=["software", "media"],
                allowed_predicates=["USES", "VIEWED"],
                structured_allowed_entity_types=["software", "media"],
                structured_allowed_predicates=["USES", "VIEWED"],
                allowed_assertion_families=["routine_profile"],
                allow_graph=True,
                allow_assertion=True,
                assertion_mode="derived",
                allowed_assertion_traits=["app.*"],
                derived_assertion_specs=[
                    {
                        "rule_id": "screen_time.recurring_app_usage",
                        "source_predicates": ["USES"],
                        "source_types": ["screen_time"],
                        "trait_family": "routine_profile",
                        "trait_name_template": "app.{object_slug}",
                        "min_observations": 3,
                        "min_distinct_days": 2,
                        "object_types": ["software"],
                        "source_domains": ["external_activity"],
                        "value_strategy": "canonical_name",
                    }
                ],
                extraction_instructions=(
                    "These events are foreground-app usage duration records.\n"
                    "Each event reports how long an app was frontmost in a time window.\n\n"
                    "Entity extraction rules:\n"
                    "- Extract the application as a `software` entity using the\n"
                    "  `display_name` field (already localized and human-friendly).\n"
                    "- Treat `canonical_id` as the stable cross-platform identifier\n"
                    "  for the same logical app; never expose the raw bundle_id or\n"
                    "  Windows executable path to the user.\n"
                    "- Extract media-centric apps (Netflix, YouTube, Spotify, etc.)\n"
                    "  with the VIEWED predicate; use USES for productivity and\n"
                    "  development tools.\n"
                    "- Skip extractions for very brief system utility interactions.\n\n"
                    "Assertion rules:\n"
                    "- Do not emit Phase 2 assertion candidates for app-usage records. "
                    "Repeated USES graph evidence may be aggregated later by the "
                    "host-owned derived app-usage rule declared in this profile."
                ),
            )
        ]

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
        """Aggregate app-usage bucket features for L3 summaries."""
        _ = summary_category, period_start, period_end
        if source_type != "screen_time" or not events:
            return None

        app_duration: Counter[str] = Counter()
        app_sessions: Counter[str] = Counter()
        display_name_by_id: dict[str, str] = {}
        representative_event_ids: list[str] = []
        total_duration_seconds = 0
        total_session_count = 0

        for event in events:
            provenance = _event_provenance(event)
            canonical_id = str(
                provenance.get("canonical_id")
                or provenance.get("bundle_id")
                or "unknown"
            ).strip() or "unknown"
            display_name = str(
                provenance.get("display_name")
                or provenance.get("app_name")
                or canonical_id
            ).strip()
            duration_seconds = _int_value(provenance, "duration_seconds")
            session_count = _int_value(provenance, "session_count")
            app_duration[canonical_id] += duration_seconds
            app_sessions[canonical_id] += session_count
            if display_name and canonical_id not in display_name_by_id:
                display_name_by_id[canonical_id] = display_name
            total_duration_seconds += duration_seconds
            total_session_count += session_count
            event_id = str(event.get("event_id") or "").strip()
            if event_id and len(representative_event_ids) < 8:
                representative_event_ids.append(event_id)

        covered_event_count = len(events)
        total_event_count = _budget_int(budget, "total_event_count", covered_event_count)
        omitted_event_count = max(0, total_event_count - covered_event_count)
        top_apps = [
            {
                "canonical_id": canonical_id,
                "app": display_name_by_id.get(canonical_id, canonical_id),
                "duration_seconds": int(seconds),
                "session_count": int(app_sessions.get(canonical_id, 0)),
            }
            for canonical_id, seconds in app_duration.most_common(6)
        ]

        summary_lines = [
            f"App usage feature coverage used {covered_event_count} hourly app buckets covering {_format_minutes(total_duration_seconds)}."
        ]
        if top_apps:
            joined = ", ".join(
                f"{item['app']} ({_format_minutes(int(item['duration_seconds']))})"
                for item in top_apps[:4]
            )
            summary_lines.append(f"Most active apps: {joined}.")
        if total_session_count:
            summary_lines.append(
                f"App usage included {total_session_count} frontmost sessions in the covered buckets."
            )
        if omitted_event_count > 0:
            summary_lines.append(
                f"App usage feature coverage used {covered_event_count} representative buckets; {omitted_event_count} additional buckets were compacted."
            )

        return {
            "feature_type": "screen_time",
            "bucket_count": covered_event_count,
            "total_event_count": total_event_count,
            "covered_event_count": covered_event_count,
            "omitted_event_count": omitted_event_count,
            "coverage_ratio": (covered_event_count / total_event_count) if total_event_count else None,
            "total_duration_seconds": total_duration_seconds,
            "total_session_count": total_session_count,
            "top_entities": [{"type": "app", **item} for item in top_apps],
            "representative_event_ids": representative_event_ids,
            "summary_lines": summary_lines,
        }

    def get_sensors(self) -> list[tuple[str, object, SensorSpec]]:
        if sys.platform not in SUPPORTED_PLATFORMS:
            return []

        settings: dict[str, Any] = {}
        sensors_settings = self.settings.get("sensors", {})
        if isinstance(sensors_settings, dict):
            settings = dict(sensors_settings.get("screen_time", {}))

        sensor = ScreenTimeTimelineSensor()
        sync_interval_minutes = int(
            settings.get("sync_interval_minutes", DEFAULT_SETTINGS["sync_interval_minutes"])
        )

        return [
            (
                "timeline.screen_time",
                sensor,
                SensorSpec(
                    sensor_id="timeline.screen_time",
                    display_name="App Usage",
                    description=(
                        "Polls the foreground app each second and emits one summary per "
                        "app per clock hour, flushed shortly after each hour ends."
                    ),
                    domain="timeline",
                    surface="timeline",
                    sync_mode="interval",
                    polling_mode="interval",
                    fields=_fields("sensors.screen_time"),
                    metadata={
                        "source_type": "screen_time",
                        "default_settings": dict(DEFAULT_SETTINGS),
                        "sync_interval_minutes": sync_interval_minutes,
                    },
                ),
            )
        ]
