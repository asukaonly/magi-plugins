"""Frontmost app usage timeline plugin."""
from __future__ import annotations

from collections import Counter
import sys
from typing import Any

from magi_plugin_sdk import ExtensionFieldOption, ExtensionFieldSpec, Plugin, SensorSpec
from magi_plugin_sdk.ingress import PluginIngressHandlerRegistration
from magi_plugin_sdk.sensors import PluginRuntimePaths

from .ingress import ScreenTimePluginIngressHandler
from .sensor import ScreenTimeTimelineSensor

DEFAULT_SETTINGS = {
    "enabled": False,
    "sync_interval_minutes": 5,
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
    """Define all settings fields for the frontmost app usage plugin."""
    return [
        ExtensionFieldSpec(
            key=f"{prefix}.enabled",
            type="switch",
            label="Enable App Usage Sync",
            description="Track frontmost app activation events and write hourly summaries to memory.",
            default=False,
            section="general",
            surface="timeline",
            order=10,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sync_interval_minutes",
            type="select",
            label="Flush Interval",
            description="How often to flush completed hourly app-usage buckets into memory.",
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
    """Registers the frontmost app usage source under the existing package id."""

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
        representative_event_ids: list[str] = []
        total_duration_seconds = 0
        total_session_count = 0

        for event in events:
            provenance = _event_provenance(event)
            app_name = str(provenance.get("app_name") or provenance.get("bundle_id") or "Unknown App").strip()
            duration_seconds = _int_value(provenance, "duration_seconds")
            session_count = _int_value(provenance, "session_count")
            app_duration[app_name] += duration_seconds
            app_sessions[app_name] += session_count
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
                "app": app,
                "duration_seconds": int(seconds),
                "session_count": int(app_sessions.get(app, 0)),
            }
            for app, seconds in app_duration.most_common(6)
        ]

        summary_lines = [
            f"App usage feature coverage used {covered_event_count} hourly app buckets covering {_format_minutes(total_duration_seconds)}."
        ]
        if top_apps:
            joined = ", ".join(f"{item['app']} ({_format_minutes(int(item['duration_seconds']))})" for item in top_apps[:4])
            summary_lines.append(f"Most active apps: {joined}.")
        if total_session_count:
            summary_lines.append(f"App usage included {total_session_count} frontmost sessions in the covered buckets.")
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

    def get_plugin_ingress_registrations(
        self,
        *,
        runtime_paths: PluginRuntimePaths,
    ) -> list[PluginIngressHandlerRegistration]:
        if sys.platform != "darwin":
            return []

        return [
            PluginIngressHandlerRegistration(
                plugin_target="screen_time",
                event_type="frontmost_app_activated",
                handler=ScreenTimePluginIngressHandler(runtime_paths=runtime_paths),
            )
        ]

    def get_sensors(self) -> list[tuple[str, object, SensorSpec]]:
        if sys.platform != "darwin":
            return []

        settings = {}
        sensors_settings = self.settings.get("sensors", {})
        if isinstance(sensors_settings, dict):
            settings = dict(sensors_settings.get("screen_time", {}))

        sensor = ScreenTimeTimelineSensor()
        sync_interval_minutes = int(settings.get("sync_interval_minutes", DEFAULT_SETTINGS["sync_interval_minutes"]))

        return [
            (
                "timeline.screen_time",
                sensor,
                SensorSpec(
                    sensor_id="timeline.screen_time",
                    display_name="App Usage",
                    description="Event-driven frontmost app usage aggregated into hourly summaries.",
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
