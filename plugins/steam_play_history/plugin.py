"""Steam play history timeline plugin."""
from __future__ import annotations

from collections import Counter
import sys
from typing import Any

from magi_plugin_sdk import ActivationFlowSpec, ExtensionFieldOption, ExtensionFieldSpec, Plugin, SensorSpec

from .reader import detect_steam_root
from .sensor import SteamPlayHistoryTimelineSensor
from .state import DEFAULT_MIN_SESSION_S, SteamPlayStateStore

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


def _activation_flow(prefix: str, t: Any) -> ActivationFlowSpec:
    return ActivationFlowSpec(
        title=t("activation.title", fallback="Enable Steam Play History"),
        description=t(
            "activation.description",
            fallback=(
                "Steam play history reveals gaming habits. Choose how the first sync should seed the timeline before "
                "this source starts running. Exact sessions are inferred only after the source is enabled."
            ),
        ),
        confirm_label=t("activation.confirm_label", fallback="Enable source"),
        cancel_label=t("activation.cancel_label", fallback="Not now"),
        enabled_key=f"{prefix}.enabled",
        configured_key=f"{prefix}.initial_sync_configured",
        fields=[
            ExtensionFieldSpec(
                key=f"{prefix}.initial_sync_policy",
                type="select",
                label=t("settings.initial_sync_policy.label", fallback="First Sync Scope"),
                description=t(
                    "settings.initial_sync_policy.description",
                    fallback="Decide how much previous Steam activity should be imported.",
                ),
                default="lookback_days",
                options=[
                    ExtensionFieldOption(
                        label=t("settings.initial_sync_policy.options.full", fallback="Import all last-played summaries"),
                        value="full",
                    ),
                    ExtensionFieldOption(
                        label=t(
                            "settings.initial_sync_policy.options.lookback_days",
                            fallback="Import recent last-played summaries",
                        ),
                        value="lookback_days",
                    ),
                    ExtensionFieldOption(
                        label=t("settings.initial_sync_policy.options.from_now", fallback="Only record play from now on"),
                        value="from_now",
                    ),
                ],
                section="activation",
                surface="timeline",
                order=10,
            ),
            ExtensionFieldSpec(
                key=f"{prefix}.initial_sync_lookback_days",
                type="number",
                label=t("settings.initial_sync_lookback_days.label", fallback="Recent Days"),
                description=t(
                    "settings.initial_sync_lookback_days.description",
                    fallback="Used when the first-sync scope is set to recent summaries.",
                ),
                default=14,
                min=1,
                section="activation",
                surface="timeline",
                order=20,
                depends_on_key=f"{prefix}.initial_sync_policy",
                depends_on_values=["lookback_days"],
            ),
        ],
    )


def _fields(prefix: str, t: Any, *, detected_steam_path: str) -> list[ExtensionFieldSpec]:
    return [
        ExtensionFieldSpec(
            key=f"{prefix}.enabled",
            type="switch",
            label=t("settings.enabled.label", fallback="Enabled"),
            description=t("settings.enabled.description", fallback="Whether Steam play history sync is active."),
            default=False,
            section="general",
            surface="timeline",
            order=10,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.steam_path",
            type="path",
            label=t("settings.steam_path.label", fallback="Steam Path"),
            description=t(
                "settings.steam_path.description",
                fallback="Optional Steam install path. Leave empty to auto-detect the local Steam folder.",
            ),
            default=detected_steam_path,
            section="general",
            surface="timeline",
            order=20,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sync_mode",
            type="select",
            label=t("settings.sync_mode.label", fallback="Sync Mode"),
            description=t("settings.sync_mode.description", fallback="How Steam play history should be synchronized."),
            default="interval",
            options=[
                ExtensionFieldOption(label=t("settings.sync_mode.options.manual", fallback="Manual"), value="manual"),
                ExtensionFieldOption(label=t("settings.sync_mode.options.interval", fallback="Interval"), value="interval"),
            ],
            section="sync",
            surface="timeline",
            order=30,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sync_interval_minutes",
            type="number",
            label=t("settings.sync_interval_minutes.label", fallback="Sync Interval (minutes)"),
            description=t("settings.sync_interval_minutes.description", fallback="How often to poll Steam playtime changes."),
            default=10,
            min=1,
            section="sync",
            surface="timeline",
            order=40,
            depends_on_key=f"{prefix}.sync_mode",
            depends_on_values=["interval"],
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.min_session_seconds",
            type="number",
            label=t("settings.min_session_seconds.label", fallback="Minimum Session Duration (seconds)"),
            description=t("settings.min_session_seconds.description", fallback="Inferred play sessions shorter than this are ignored."),
            default=DEFAULT_MIN_SESSION_S,
            min=60,
            section="sync",
            surface="timeline",
            order=50,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.idle_timeout_minutes",
            type="number",
            label=t("settings.idle_timeout_minutes.label", fallback="Idle Timeout (minutes)"),
            description=t(
                "settings.idle_timeout_minutes.description",
                fallback="A session is closed after this many minutes without additional Steam playtime.",
            ),
            default=15,
            min=2,
            section="sync",
            surface="timeline",
            order=60,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.max_items_per_sync",
            type="number",
            label=t("settings.max_items_per_sync.label", fallback="Max Items Per Sync"),
            description=t("settings.max_items_per_sync.description", fallback="Maximum number of Steam records to emit per sync."),
            default=500,
            min=1,
            section="sync",
            surface="timeline",
            order=70,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.excluded_keywords",
            type="tags",
            label=t("settings.excluded_keywords.label", fallback="Excluded Game Keywords"),
            description=t(
                "settings.excluded_keywords.description",
                fallback="Game names containing these case-insensitive keywords are skipped before AI analysis.",
            ),
            default=[],
            section="privacy",
            surface="timeline",
            order=80,
            placeholder="e.g. private",
        ),
    ]


class SteamPlayHistoryPlugin(Plugin):
    """Registers the Steam play-history timeline source."""

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

    def get_sensors(self) -> list[tuple[str, object, SensorSpec]]:
        if not (sys.platform == "win32" or sys.platform == "darwin" or sys.platform.startswith("linux")):
            return []

        settings = {}
        sensors_settings = self.settings.get("sensors", {})
        if isinstance(sensors_settings, dict):
            settings = dict(sensors_settings.get("steam_play_history", {}))

        min_session_s = int(settings.get("min_session_seconds", DEFAULT_SETTINGS["min_session_seconds"]))
        idle_timeout_minutes = int(settings.get("idle_timeout_minutes", DEFAULT_SETTINGS["idle_timeout_minutes"]))
        sync_interval = int(settings.get("sync_interval_minutes", DEFAULT_SETTINGS["sync_interval_minutes"]))
        configured_steam_path = str(settings.get("steam_path") or DEFAULT_SETTINGS["steam_path"])
        detected_steam_path = str(detect_steam_root(configured_steam_path) or configured_steam_path or "")

        sensor = SteamPlayHistoryTimelineSensor(
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
                sensor,
                SensorSpec(
                    sensor_id="timeline.steam_play_history",
                    display_name=self.t("steam_play_history.name", fallback="Steam Play History"),
                    description=self.t(
                        "steam_play_history.description",
                        fallback="Steam gameplay sessions inferred from local Steam playtime changes.",
                    ),
                    domain="timeline",
                    surface="timeline",
                    sync_mode=str(settings.get("sync_mode", DEFAULT_SETTINGS["sync_mode"])),
                    polling_mode="interval",
                    fields=_fields(
                        "sensors.steam_play_history",
                        self.t,
                        detected_steam_path=detected_steam_path,
                    ),
                    metadata={
                        "source_type": "steam_play_history",
                        "default_settings": dict(DEFAULT_SETTINGS),
                        "sync_interval_minutes": sync_interval,
                        "activation_flow": _activation_flow("sensors.steam_play_history", self.t).model_dump(),
                    },
                ),
            )
        ]
