"""Terminal History timeline plugin."""
from __future__ import annotations

from collections import Counter
import sys
from typing import Any

from magi_plugin_sdk import (
    ExtractionProfileSpec,
    Plugin,
    SourceSpec,
)

from .reader import TerminalHistoryReader
from .source import TerminalHistorySource

DEFAULT_SETTINGS = {
    "enabled": False,
    "sync_interval_minutes": 15,
    "initial_sync_policy": "lookback_days",
    "initial_sync_lookback_days": 7,
    "initial_sync_configured": False,
    "sensitive_mode": "redact",
    "sensitive_keywords": [],
    "dedup_window_seconds": 60,
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
    activity_snapshot = metadata.get("activity_snapshot")
    if not isinstance(activity_snapshot, dict):
        return {}
    provenance = activity_snapshot.get("provenance")
    return provenance if isinstance(provenance, dict) else {}


def _event_text(event: dict[str, Any]) -> str:
    metadata = event.get("metadata_json")
    activity_snapshot = metadata.get("activity_snapshot") if isinstance(metadata, dict) else None
    snapshot_text = ""
    if isinstance(activity_snapshot, dict):
        snapshot_text = str(
            activity_snapshot.get("title") or activity_snapshot.get("summary") or ""
        )
    return str(
        event.get("content")
        or event.get("title")
        or event.get("summary")
        or snapshot_text
        or ""
    )


def _command_family(event: dict[str, Any]) -> str | None:
    text = _event_text(event).strip()
    if not text:
        return None
    for marker in ("命令：", "Command:", "command:", "$ "):
        if marker in text:
            text = text.split(marker, 1)[1].strip()
            break
    text = text.splitlines()[0]
    text = text.split(" @ ", 1)[0].strip()
    if not text:
        return None
    tokens = [token.strip() for token in text.replace(";", " ").replace("&&", " ").split() if token.strip()]
    while tokens and tokens[0] in {"sudo", "env", "time", "command", "builtin"}:
        tokens.pop(0)
    if not tokens:
        return None
    command = tokens[0].rsplit("/", 1)[-1]
    return command[:48] or None


def _event_id(event: dict[str, Any]) -> str | None:
    value = str(event.get("event_id") or "").strip()
    return value or None


class TerminalHistoryPlugin(Plugin):
    """Registers the Terminal History timeline source."""

    def get_extraction_profiles(self) -> list[ExtractionProfileSpec]:
        return [
            ExtractionProfileSpec(
                profile_id="source.terminal_history",
                source_types=["terminal_history"],
                allowed_entity_types=["software", "technology"],
                allowed_predicates=["EXECUTED", "USED", "USES"],
                structured_allowed_entity_types=["software", "technology"],
                structured_allowed_predicates=["EXECUTED", "USED", "USES"],
                allowed_assertion_families=["routine_profile"],
                allow_graph=True,
                allow_assertion=True,
                allowed_assertion_traits=["routine.tool.*"],
                derived_assertion_specs=[
                    {
                        "rule_id": "terminal_history.recurring_tool",
                        "source_predicates": ["EXECUTED"],
                        "source_types": ["terminal_history"],
                        "trait_family": "routine_profile",
                        "trait_name_template": "routine.tool.{object_slug}",
                        "min_observations": 3,
                        "min_distinct_days": 2,
                        "signal_preset": "sustained_engagement",
                        "durable_permitted": True,
                        "durable_min_observations": 8,
                        "durable_min_distinct_days": 4,
                        "durable_min_span_days": 21,
                        "object_types": ["software"],
                        "source_domains": ["external_activity"],
                        "value_strategy": "canonical_name",
                    }
                ],
                extraction_instructions=(
                    "These events are shell command executions. Extract meaningful tool usage\n"
                    "patterns, not individual commands.\n\n"
                    "Entity extraction rules:\n"
                    "- Extract CLI tools and package managers as `software` entities\n"
                    "  (e.g., docker, kubectl, npm, brew).\n"
                    "- Extract programming runtimes as `technology` entities\n"
                    "  (e.g., python, node, rust).\n"
                    "- USES: for recurring tool usage patterns.\n"
                    "- EXECUTED: for specific tool invocations.\n"
                    "- Do NOT extract arguments, file paths, environment variables,\n"
                    "  or shell builtins (cd, ls, cat) as entities.\n"
                    "- Be SELECTIVE: skip noise commands and focus on tools that reveal\n"
                    "  the user's technical stack and workflow.\n\n"
                    "Assertion rules:\n"
                    "- Do not emit Phase 2 assertion candidates for terminal events. Repeated\n"
                    "  EXECUTED graph evidence may be aggregated later by the host-owned\n"
                    "  derived tool rule declared in this profile."
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
        """Aggregate command-family features without exposing full commands."""
        _ = summary_category, period_start, period_end
        if source_type != "terminal_history" or not events:
            return None

        shell_counter: Counter[str] = Counter()
        command_counter: Counter[str] = Counter()
        long_command_count = 0
        representative_event_ids: list[str] = []

        for event in events:
            provenance = _event_provenance(event)
            shell = str(provenance.get("shell") or "unknown").strip() or "unknown"
            shell_counter[shell] += 1
            try:
                command_length = int(provenance.get("command_length") or 0)
            except (TypeError, ValueError):
                command_length = 0
            if command_length >= 120:
                long_command_count += 1
            command = _command_family(event)
            if command:
                command_counter[command] += 1
            event_id = _event_id(event)
            if event_id and len(representative_event_ids) < 8:
                representative_event_ids.append(event_id)

        covered_event_count = len(events)
        total_event_count = _budget_int(budget, "total_event_count", covered_event_count)
        omitted_event_count = max(0, total_event_count - covered_event_count)
        top_commands = [
            {"command_family": command, "event_count": count}
            for command, count in command_counter.most_common(6)
        ]
        top_shells = [
            {"shell": shell, "event_count": count}
            for shell, count in shell_counter.most_common(4)
        ]

        summary_lines = [
            f"Terminal feature coverage used {covered_event_count} commands across {len(shell_counter)} shells."
        ]
        if top_commands:
            joined = ", ".join(f"{item['command_family']} ({item['event_count']})" for item in top_commands[:4])
            summary_lines.append(f"Common command families: {joined}.")
        if top_shells:
            joined = ", ".join(f"{item['shell']} ({item['event_count']})" for item in top_shells[:3])
            summary_lines.append(f"Terminal shells represented: {joined}.")
        if long_command_count:
            summary_lines.append(f"Long-form commands appeared {long_command_count} times in the covered events.")
        if omitted_event_count > 0:
            summary_lines.append(
                f"Terminal feature coverage used {covered_event_count} representative commands; {omitted_event_count} additional commands were compacted."
            )

        return {
            "feature_type": "terminal_history",
            "event_count": covered_event_count,
            "total_event_count": total_event_count,
            "covered_event_count": covered_event_count,
            "omitted_event_count": omitted_event_count,
            "coverage_ratio": (covered_event_count / total_event_count) if total_event_count else None,
            "shell_count": len(shell_counter),
            "top_entities": [{"type": "command_family", **item} for item in top_commands],
            "top_shells": top_shells,
            "long_command_count": long_command_count,
            "representative_event_ids": representative_event_ids,
            "summary_lines": summary_lines,
        }

    def get_sources(self) -> list[tuple[str, object, SourceSpec]]:
        """Get source specifications for Terminal History.

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
            settings = dict(sources_settings.get("terminal_history", {}))

        source_enabled = bool(settings.get("enabled", DEFAULT_SETTINGS["enabled"]))

        # Check history availability (but still return source spec even if not available)
        reader = None
        if source_enabled:
            try:
                reader = TerminalHistoryReader()
                if not reader.is_available():
                    reader = None
            except Exception:
                reader = None

        # Create source (reader may be None if not available)
        source = TerminalHistorySource(
            retention_mode=str(settings.get("default_retention_mode", DEFAULT_SETTINGS["default_retention_mode"])),
            reader=reader,
        )

        # Get sync interval
        sync_interval_minutes = settings.get("sync_interval_minutes", DEFAULT_SETTINGS["sync_interval_minutes"])

        return [
            (
                "timeline.terminal_history",
                source,
                SourceSpec(
                    source_id="timeline.terminal_history",
                    display_name="Terminal History",
                    description="Terminal command history ingestion for the timeline.",
                    domain="timeline",
                    surface="timeline",
                    sync_mode="interval",
                    polling_mode="interval",
                    fields=[
                        field.model_copy(deep=True)
                        for field in sorted(self.manifest.settings_fields, key=lambda field: field.order)
                        if field.surface == "timeline" and field.section != "activation"
                    ],
                    metadata={
                        "source_type": "terminal_history",
                        "default_settings": {
                            field.key.rsplit(".", 1)[-1]: field.model_copy(deep=True).default
                            for field in self.manifest.settings_fields
                            if field.key.startswith("sources.") and field.type != "secret"
                        },
                        "sync_interval_minutes": sync_interval_minutes,
                        "activation_flow": self.manifest.activation_flow.model_dump() if self.manifest.activation_flow is not None else None,
                    },
                ),
            )
        ]
