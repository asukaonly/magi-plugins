"""Terminal History timeline plugin."""
from __future__ import annotations

from collections import Counter
import sys
from typing import Any

from magi_plugin_sdk import (
    ActivationFlowSpec,
    ExtensionFieldOption,
    ExtensionFieldSpec,
    Plugin,
    SensorSpec,
)

from .filters import BUILTIN_SENSITIVE_KEYWORDS
from .reader import TerminalHistoryReader
from .sensor import TerminalHistorySensor

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
    timeline = metadata.get("timeline")
    if not isinstance(timeline, dict):
        return {}
    provenance = timeline.get("provenance")
    return provenance if isinstance(provenance, dict) else {}


def _event_text(event: dict[str, Any]) -> str:
    metadata = event.get("metadata_json")
    timeline = metadata.get("timeline") if isinstance(metadata, dict) else None
    timeline_text = ""
    if isinstance(timeline, dict):
        timeline_text = str(timeline.get("title") or timeline.get("summary") or "")
    return str(event.get("content") or event.get("title") or event.get("summary") or timeline_text or "")


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


def _activation_flow(prefix: str) -> ActivationFlowSpec:
    """Define activation flow for first-time setup."""
    return ActivationFlowSpec(
        title="Enable Terminal History",
        description=(
            "Terminal history contains commands you've executed. "
            "Choose how much history should be imported when this source is enabled for the first time."
        ),
        confirm_label="Enable source",
        cancel_label="Not now",
        enabled_key=f"{prefix}.enabled",
        configured_key=f"{prefix}.initial_sync_configured",
        fields=[
            ExtensionFieldSpec(
                key=f"{prefix}.initial_sync_policy",
                type="select",
                label="First Sync Scope",
                description="Decide how much command history should be imported.",
                default="lookback_days",
                options=[
                    ExtensionFieldOption(label="Sync full history", value="full"),
                    ExtensionFieldOption(label="Sync recent days", value="lookback_days"),
                    ExtensionFieldOption(label="Only new commands from now", value="from_now"),
                ],
                section="activation",
                surface="timeline",
                order=10,
            ),
            ExtensionFieldSpec(
                key=f"{prefix}.initial_sync_lookback_days",
                type="number",
                label="Recent Days",
                description="Used when the first-sync scope is set to recent days.",
                default=7,
                section="activation",
                surface="timeline",
                order=20,
                depends_on_key=f"{prefix}.initial_sync_policy",
                depends_on_values=["lookback_days"],
            ),
        ],
    )


def _fields(prefix: str) -> list[ExtensionFieldSpec]:
    """Define all settings fields for the Terminal History plugin."""
    return [
        ExtensionFieldSpec(
            key=f"{prefix}.enabled",
            type="switch",
            label="Enabled",
            description="Whether terminal history sync is active.",
            default=False,
            section="general",
            surface="timeline",
            order=10,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sync_interval_minutes",
            type="number",
            label="Sync Interval (minutes)",
            description="How often to check for new terminal commands.",
            default=15,
            min=1,
            max=1440,
            section="general",
            surface="timeline",
            order=20,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.default_retention_mode",
            type="select",
            label="Retention Mode",
            description="How terminal history data should be retained.",
            default="analyze_only",
            options=[
                ExtensionFieldOption(label="Analyze Only", value="analyze_only"),
                ExtensionFieldOption(label="Full Retention", value="full"),
            ],
            section="retention",
            surface="timeline",
            order=30,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sensitive_mode",
            type="select",
            label="Sensitive Command Mode",
            description="How to handle commands containing sensitive information.",
            default="redact",
            options=[
                ExtensionFieldOption(label="Redact sensitive parts", value="redact"),
                ExtensionFieldOption(label="Block entire command", value="block"),
            ],
            section="privacy",
            surface="timeline",
            order=40,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sensitive_keywords",
            type="tags",
            label="Additional Sensitive Keywords",
            description="Extra keywords to detect sensitive commands (built-in: password, token, api_key, etc.)",
            default=[],
            section="privacy",
            surface="timeline",
            order=50,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.dedup_window_seconds",
            type="number",
            label="Dedup Window (seconds)",
            description="Time window for session-based command deduplication.",
            default=60,
            min=0,
            max=3600,
            section="general",
            surface="timeline",
            order=60,
        ),
    ]


class TerminalHistoryPlugin(Plugin):
    """Registers the Terminal History timeline source."""

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

    def get_sensors(self) -> list[tuple[str, object, SensorSpec]]:
        """Get sensor specifications for Terminal History.

        Returns:
            List of sensor tuples (sensor_id, sensor_instance, sensor_spec)
        """
        # Check platform - only supported on Darwin
        if sys.platform != "darwin":
            return []

        # Get settings
        settings = {}
        sensors_settings = self.settings.get("sensors", {})
        if isinstance(sensors_settings, dict):
            settings = dict(sensors_settings.get("terminal_history", {}))

        source_enabled = bool(settings.get("enabled", DEFAULT_SETTINGS["enabled"]))

        # Check history availability (but still return sensor spec even if not available)
        reader = None
        if source_enabled:
            try:
                reader = TerminalHistoryReader()
                if not reader.is_available():
                    reader = None
            except Exception:
                reader = None

        # Create sensor (reader may be None if not available)
        sensor = TerminalHistorySensor(
            retention_mode=str(settings.get("default_retention_mode", DEFAULT_SETTINGS["default_retention_mode"])),
            reader=reader,
        )

        # Get sync interval
        sync_interval_minutes = settings.get("sync_interval_minutes", DEFAULT_SETTINGS["sync_interval_minutes"])

        return [
            (
                "timeline.terminal_history",
                sensor,
                SensorSpec(
                    sensor_id="timeline.terminal_history",
                    display_name="Terminal History",
                    description="Terminal command history ingestion for the timeline.",
                    domain="timeline",
                    surface="timeline",
                    sync_mode="interval",
                    polling_mode="interval",
                    fields=_fields("sensors.terminal_history"),
                    metadata={
                        "source_type": "terminal_history",
                        "default_settings": dict(DEFAULT_SETTINGS),
                        "sync_interval_minutes": sync_interval_minutes,
                        "activation_flow": _activation_flow("sensors.terminal_history").model_dump(),
                    },
                ),
            )
        ]
