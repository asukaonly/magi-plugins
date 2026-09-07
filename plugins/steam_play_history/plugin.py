"""Steam play history timeline plugin."""
from __future__ import annotations

from collections import Counter
import sys
from typing import Any

from magi_plugin_sdk import ActivationFlowSpec, ExtensionFieldSpec, ExtractionProfileSpec, Plugin, SourceSpec

from .source import SteamPlayHistoryTimelineSource
from .state import DEFAULT_MIN_SESSION_S, SteamPlayStateStore

STEAM_L2_DERIVED_RULE = {
    "rule_id": "steam_play_history.viewed_interest",
    "source_predicates": ["VIEWED"],
    "source_types": ["steam_play_history"],
    "trait_family": "interest_profile",
    "trait_name_template": "interest.{object_slug}",
    "min_observations": 2,
    "min_distinct_days": 2,
    "signal_preset": "sustained_engagement",
    "durable_permitted": True,
    "durable_min_observations": 6,
    "durable_min_distinct_days": 3,
    "durable_min_span_days": 14,
    "object_types": ["media"],
    "source_domains": ["external_activity"],
    "value_strategy": "canonical_name",
}

DEFAULT_SETTINGS = {
    "enabled": False,
    "sync_mode": "interval",
    "sync_interval_minutes": 10,
    "steam_path": "",
    "max_items_per_sync": 500,
    "min_session_seconds": DEFAULT_MIN_SESSION_S,
    "idle_timeout_minutes": 15,
    "initial_sync_policy": "lookback_days",
    "initial_sync_lookback_days": 14,
    "initial_sync_configured": False,
    "excluded_keywords": [],
    "default_retention_mode": "analyze_only",
}

GAME_RECORDS_CAPABILITY_METADATA = {
    "capability_id": "game_records",
    "capability_display_name": "Game Records",
    "capability_description": "Manage game activity sources that feed the timeline.",
    "entry_id": "steam",
    "entry_display_name": "Steam",
    "entry_description": "Local Steam game activity and play sessions.",
    "entry_order": 10,
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


def _localize_field(field: ExtensionFieldSpec, t: Any) -> ExtensionFieldSpec:
    """Translate presentation text while preserving the reviewed field contract."""
    field = field.model_copy(deep=True)
    key = f"settings.{field.key.rsplit('.', 1)[-1]}"
    for attribute in ("label", "description"):
        setattr(field, attribute, t(f"{key}.{attribute}", fallback=getattr(field, attribute)))
    for option in field.options:
        option.label = t(f"{key}.options.{option.value}", fallback=option.label)
    return field


def _localized_fields(fields: list[ExtensionFieldSpec], t: Any) -> list[ExtensionFieldSpec]:
    return [
        _localize_field(field, t)
        for field in sorted(fields, key=lambda field: field.order)
        if field.surface == "timeline" and field.section != "activation"
    ]


def _localized_activation(flow: ActivationFlowSpec, t: Any) -> ActivationFlowSpec:
    flow = flow.model_copy(deep=True)
    for attribute in ("title", "description", "confirm_label", "cancel_label"):
        setattr(flow, attribute, t(f"activation.{attribute}", fallback=getattr(flow, attribute)))
    flow.fields = [_localize_field(field, t) for field in flow.fields]
    return flow


class SteamPlayHistoryPlugin(Plugin):
    """Registers the Steam play-history timeline source."""

    def get_extraction_profiles(self) -> list[ExtractionProfileSpec]:
        return [
            ExtractionProfileSpec(
                profile_id="source.steam_play_history",
                source_types=["steam_play_history"],
                allowed_entity_types=["media", "software"],
                allowed_predicates=["VIEWED", "INTERESTED_IN"],
                structured_allowed_entity_types=["media", "software"],
                structured_allowed_predicates=["VIEWED", "INTERESTED_IN"],
                allowed_assertion_families=["interest_profile"],
                allow_graph=True,
                allow_assertion=True,
                allowed_assertion_traits=["interest.*"],
                derived_assertion_specs=[dict(STEAM_L2_DERIVED_RULE)],
                extraction_instructions=(
                    "These events are local Steam game play records. Treat them as passive "
                    "activity evidence, not explicit user preference claims. Use VIEWED for "
                    "the played game as media evidence. Do not emit Phase 2 assertion "
                    "candidates; repeated VIEWED evidence is aggregated by the host-owned "
                    "derived game interest rule declared in this profile."
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
        """Aggregate Steam play-session features for temporal summaries."""
        _ = summary_category, period_start, period_end
        if source_type != "steam_play_history" or not events:
            return None

        game_duration: Counter[str] = Counter()
        game_sessions: Counter[str] = Counter()
        source_counter: Counter[str] = Counter()
        total_duration_seconds = 0
        representative_event_ids: list[str] = []

        for event in events:
            provenance = _event_provenance(event)
            game_name = str(provenance.get("game_name") or provenance.get("appid") or "Steam game").strip()
            duration_seconds = _int_value(provenance, "duration_seconds")
            if duration_seconds > 0:
                game_duration[game_name] += duration_seconds
                total_duration_seconds += duration_seconds
            game_sessions[game_name] += 1
            source_counter[str(provenance.get("source") or "unknown")] += 1
            event_id = str(event.get("event_id") or "").strip()
            if event_id and len(representative_event_ids) < 8:
                representative_event_ids.append(event_id)

        covered_event_count = len(events)
        total_event_count = _budget_int(budget, "total_event_count", covered_event_count)
        omitted_event_count = max(0, total_event_count - covered_event_count)
        top_games = [
            {
                "game": game,
                "duration_seconds": int(seconds),
                "session_count": int(game_sessions.get(game, 0)),
            }
            for game, seconds in game_duration.most_common(6)
        ]
        if not top_games:
            top_games = [
                {"game": game, "duration_seconds": 0, "session_count": count}
                for game, count in game_sessions.most_common(6)
            ]

        summary_lines = [
            f"Steam feature coverage used {covered_event_count} play records totaling {_format_minutes(total_duration_seconds)}."
        ]
        if top_games:
            joined = ", ".join(
                f"{item['game']} ({_format_minutes(int(item['duration_seconds']))})"
                for item in top_games[:4]
            )
            summary_lines.append(f"Top Steam games by inferred playtime: {joined}.")
        if source_counter:
            joined = ", ".join(f"{source} ({count})" for source, count in source_counter.most_common(3))
            summary_lines.append(f"Steam data sources represented: {joined}.")
        if omitted_event_count > 0:
            summary_lines.append(
                f"Steam feature coverage used {covered_event_count} representative records; {omitted_event_count} additional records were compacted."
            )

        return {
            "feature_type": "steam_play_history",
            "record_count": covered_event_count,
            "total_event_count": total_event_count,
            "covered_event_count": covered_event_count,
            "omitted_event_count": omitted_event_count,
            "coverage_ratio": (covered_event_count / total_event_count) if total_event_count else None,
            "total_duration_seconds": total_duration_seconds,
            "top_entities": [{"type": "game", **item} for item in top_games],
            "top_games": top_games,
            "representative_event_ids": representative_event_ids,
            "summary_lines": summary_lines,
        }

    def get_sources(self) -> list[tuple[str, object, SourceSpec]]:
        if not (sys.platform == "win32" or sys.platform == "darwin" or sys.platform.startswith("linux")):
            return []

        settings = {}
        sources_settings = self.settings.get("sources", {})
        if isinstance(sources_settings, dict):
            settings = dict(sources_settings.get("steam_play_history", {}))

        min_session_s = int(settings.get("min_session_seconds", DEFAULT_SETTINGS["min_session_seconds"]))
        idle_timeout_minutes = int(settings.get("idle_timeout_minutes", DEFAULT_SETTINGS["idle_timeout_minutes"]))
        sync_interval = int(settings.get("sync_interval_minutes", DEFAULT_SETTINGS["sync_interval_minutes"]))
        configured_steam_path = str(settings.get("steam_path") or DEFAULT_SETTINGS["steam_path"])

        source = SteamPlayHistoryTimelineSource(
            state_store=SteamPlayStateStore(
                idle_timeout_s=max(60, idle_timeout_minutes * 60),
                min_session_s=min_session_s,
            ),
            retention_mode=str(settings.get("default_retention_mode") or DEFAULT_SETTINGS["default_retention_mode"]),
            steam_path=configured_steam_path,
            account_id=str(settings.get("account_id") or "auto"),
        )

        return [
            (
                "timeline.steam_play_history",
                source,
                SourceSpec(
                    source_id="timeline.steam_play_history",
                    display_name=self.t("steam_play_history.name", fallback="Steam"),
                    description=self.t(
                        "steam_play_history.description",
                        fallback="Steam game records inferred from local Steam playtime changes.",
                    ),
                    domain="timeline",
                    surface="timeline",
                    sync_mode=str(settings.get("sync_mode", DEFAULT_SETTINGS["sync_mode"])),
                    polling_mode="interval",
                    fields=_localized_fields(self.manifest.settings_fields, self.t),
                    metadata={
                        "source_type": "steam_play_history",
                        "default_settings": {
                            field.key.rsplit(".", 1)[-1]: field.model_copy(deep=True).default
                            for field in self.manifest.settings_fields
                            if field.key.startswith("sources.") and field.type != "secret"
                        },
                        "sync_interval_minutes": sync_interval,
                        "activation_flow": _localized_activation(self.manifest.activation_flow, self.t).model_dump() if self.manifest.activation_flow is not None else None,
                        **GAME_RECORDS_CAPABILITY_METADATA,
                    },
                ),
            )
        ]
