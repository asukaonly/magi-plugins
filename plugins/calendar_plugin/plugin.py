"""Calendar timeline plugin."""
from __future__ import annotations

from collections import Counter
import sys
from typing import Any

from magi_plugin_sdk import (
    ExtractionProfileSpec,
    Plugin,
    PluginSettingsResourceSpec,
    SourceSpec,
)

from .reader import EventKitReader
from .source import CalendarTimelineSource

DEFAULT_SETTINGS = {
    "enabled": False,
    "authorization_configured": False,
    "sync_mode": "interval",
    "sync_interval_minutes": "30",
    "lookback_days": 30,
    "recurring_expansion_days": 30,
    "default_retention_mode": "full",
    "selected_calendar_ids": [],
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
    activity_snapshot = metadata.get("activity_snapshot")
    if not isinstance(activity_snapshot, dict):
        return {}
    provenance = activity_snapshot.get("provenance")
    return provenance if isinstance(provenance, dict) else {}


def _event_id(event: dict[str, Any]) -> str | None:
    value = str(event.get("event_id") or "").strip()
    return value or None


class CalendarPlugin(Plugin):
    """Registers the Calendar timeline source."""

    def get_extraction_profiles(self) -> list[ExtractionProfileSpec]:
        return [
            ExtractionProfileSpec(
                profile_id="source.calendar",
                source_types=["calendar"],
                allowed_entity_types=["activity", "event", "place", "organization"],
                allowed_predicates=["ATTENDED", "PLANS_TO", "VISITED"],
                allow_graph=True,
                allow_assertion=False,
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
        """Aggregate calendar-local facts for L3 temporal summaries."""
        _ = summary_category, period_start, period_end
        if source_type != "calendar" or not events:
            return None

        calendar_counter: Counter[str] = Counter()
        location_counter: Counter[str] = Counter()
        all_day_count = 0
        recurring_count = 0
        participant_event_count = 0
        participant_total = 0
        representative_event_ids: list[str] = []

        for event in events:
            provenance = _event_provenance(event)
            calendar_name = str(provenance.get("calendar_name") or "").strip()
            if calendar_name:
                calendar_counter[calendar_name] += 1
            location = str(provenance.get("location") or "").strip()
            if location:
                location_counter[location] += 1
            if bool(provenance.get("is_all_day")):
                all_day_count += 1
            if bool(provenance.get("is_recurring")):
                recurring_count += 1
            try:
                participant_count = int(provenance.get("participant_count") or 0)
            except (TypeError, ValueError):
                participant_count = 0
            if participant_count > 0:
                participant_event_count += 1
                participant_total += participant_count
            event_id = _event_id(event)
            if event_id and len(representative_event_ids) < 8:
                representative_event_ids.append(event_id)

        covered_event_count = len(events)
        total_event_count = _budget_int(budget, "total_event_count", covered_event_count)
        omitted_event_count = max(0, total_event_count - covered_event_count)
        top_calendars = [
            {"calendar": name, "event_count": count}
            for name, count in calendar_counter.most_common(5)
        ]
        top_locations = [
            {"location": name, "event_count": count}
            for name, count in location_counter.most_common(5)
        ]

        summary_lines = [
            f"Calendar feature coverage used {covered_event_count} events across {len(calendar_counter)} calendars."
        ]
        if top_calendars:
            joined = ", ".join(f"{item['calendar']} ({item['event_count']})" for item in top_calendars[:3])
            summary_lines.append(f"Most active calendars: {joined}.")
        if top_locations:
            joined = ", ".join(f"{item['location']} ({item['event_count']})" for item in top_locations[:3])
            summary_lines.append(f"Calendar locations surfaced: {joined}.")
        if recurring_count or all_day_count:
            summary_lines.append(
                f"Calendar structure included {recurring_count} recurring events and {all_day_count} all-day events."
            )
        if participant_event_count:
            summary_lines.append(
                f"Participant-bearing events appeared {participant_event_count} times with {participant_total} listed participants."
            )
        if omitted_event_count > 0:
            summary_lines.append(
                f"Calendar feature coverage used {covered_event_count} representative events; {omitted_event_count} additional events were compacted."
            )

        return {
            "feature_type": "calendar",
            "event_count": covered_event_count,
            "total_event_count": total_event_count,
            "covered_event_count": covered_event_count,
            "omitted_event_count": omitted_event_count,
            "coverage_ratio": (covered_event_count / total_event_count) if total_event_count else None,
            "calendar_count": len(calendar_counter),
            "all_day_event_count": all_day_count,
            "recurring_event_count": recurring_count,
            "participant_event_count": participant_event_count,
            "participant_total": participant_total,
            "top_entities": [{"type": "calendar", **item} for item in top_calendars],
            "top_locations": top_locations,
            "representative_event_ids": representative_event_ids,
            "summary_lines": summary_lines,
        }

    def get_settings_resources(self) -> list[PluginSettingsResourceSpec]:
        return [entry.model_copy(deep=True) for entry in self.manifest.settings_resources]

    def read_settings_resource(self, resource_name: str) -> Any:
        if resource_name != "calendar_lists":
            raise KeyError(resource_name)
        reader = EventKitReader()
        calendar_entries = reader.list_calendars()
        grouped: dict[str, dict[str, Any]] = {}
        for entry in calendar_entries:
            group = grouped.setdefault(
                entry.source_id,
                {
                    "group_id": entry.source_id,
                    "label": entry.source_title,
                    "items": [],
                },
            )
            group["items"].append(
                {
                    "item_id": entry.calendar_id,
                    "label": entry.title,
                    "description": "",
                    "accent_color": entry.accent_color,
                }
            )
        return {"groups": list(grouped.values())}

    def get_sources(self) -> list[tuple[str, object, SourceSpec]]:
        """Get source specifications for Calendar.

        Returns:
            List of source tuples (source_id, source_instance, source_spec)
        """
        # Check platform - only supported on Darwin
        if sys.platform != "darwin":
            return []

        # Get settings
        settings = {}
        sources_settings = self.settings.get("sources", {})
        if isinstance(sources_settings, dict):
            settings = dict(sources_settings.get("calendar", {}))

        source_enabled = bool(settings.get("enabled", DEFAULT_SETTINGS["enabled"]))

        # Check EventKit availability (but still return source spec even if not available)
        reader = None
        if source_enabled:
            try:
                reader = EventKitReader()
                if not reader.is_available():
                    reader = None
            except Exception:
                reader = None

        # Create source (reader may be None if not available)
        source = CalendarTimelineSource(
            retention_mode=DEFAULT_SETTINGS["default_retention_mode"],
            reader=reader,
        )

        # Prepare sync mode
        sync_mode = str(settings.get("sync_mode", DEFAULT_SETTINGS["sync_mode"]))
        sync_interval_minutes = settings.get("sync_interval_minutes", DEFAULT_SETTINGS["sync_interval_minutes"])

        return [
            (
                "timeline.calendar",
                source,
                SourceSpec(
                    source_id="timeline.calendar",
                    display_name="Calendar",
                    description="Calendar event ingestion for the timeline.",
                    domain="timeline",
                    surface="timeline",
                    sync_mode=sync_mode,
                    polling_mode=sync_mode,
                    fields=[
                        field.model_copy(deep=True)
                        for field in sorted(self.manifest.settings_fields, key=lambda field: field.order)
                        if field.surface == "timeline" and field.section != "activation"
                    ],
                    metadata={
                        "source_type": "calendar",
                        "default_settings": {
                            field.key.rsplit(".", 1)[-1]: field.model_copy(deep=True).default
                            for field in self.manifest.settings_fields
                            if field.key.startswith("sources.") and field.type != "secret"
                        },
                        "settings_ui_blocks": [block.model_dump() for block in self.manifest.settings_ui_blocks],
                        "sync_interval_minutes": sync_interval_minutes,
                        "activation_flow": self.manifest.activation_flow.model_dump() if self.manifest.activation_flow is not None else None,
                    },
                ),
            )
        ]
