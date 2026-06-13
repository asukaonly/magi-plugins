# plugin.py
"""Coding-agent history plugin.

Registers a single pull-sync sensor that ingests the user's OWN turns from AI
coding-agent transcripts (Claude Code, Codex) so L2 mines them into the user's
professional profile. The authorship crux lives in ``sensor.py``
(``author_type="user"``); this module wires the sensor + its settings surface +
the first-enable ``activation_flow`` (mirrors git_activity / photo-library).

The sensor is registered unconditionally (not gated on ``enabled``): it reads its
own settings at sync time and returns an empty result when no ``source_paths`` are
configured, so the host always has the spec available to drive the install panel.
"""
from __future__ import annotations

from typing import Any

from magi_plugin_sdk import (
    ActivationFlowSpec,
    ExtensionFieldSpec,
    Plugin,
    SensorSpec,
)

from .sensor import CodingAgentHistorySensor

_PREFIX = "sensors.coding_agent_history"
_DEFAULT_SOURCE_PATHS = ["~/.claude/projects", "~/.codex"]

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "source_paths": list(_DEFAULT_SOURCE_PATHS),
    "initial_sync_lookback_days": 30,
    "sync_interval_minutes": 30,
    "initial_sync_configured": False,
}


def _fields(prefix: str) -> list[ExtensionFieldSpec]:
    """Ongoing settings surface (shown after the source is enabled)."""
    return [
        ExtensionFieldSpec(
            key=f"{prefix}.enabled",
            type="switch",
            label="Enabled",
            description="Whether the coding-agent history sensor is active.",
            default=False,
            section="general",
            surface="timeline",
            order=10,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.source_paths",
            type="path",
            label="Transcript Folders",
            description="Folders to scan for coding-agent transcripts (e.g. ~/.claude/projects, ~/.codex).",
            default=list(_DEFAULT_SOURCE_PATHS),
            required=True,
            section="general",
            surface="timeline",
            order=20,
            placeholder="~/.claude/projects",
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.initial_sync_lookback_days",
            type="number",
            label="First-sync window (days)",
            description="First sync ingests sessions from the last N days; raise it to import more history.",
            default=30,
            min=1,
            max=3650,
            section="general",
            surface="timeline",
            order=30,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sync_interval_minutes",
            type="number",
            label="Sync Interval (minutes)",
            description="How often to rescan transcript folders for new sessions.",
            default=30,
            min=5,
            max=1440,
            section="general",
            surface="timeline",
            order=40,
        ),
    ]


def _build_activation_flow(prefix: str) -> ActivationFlowSpec:
    """First-enable consent flow rendered by the host install panel.

    Reuses the shipped activation panel (same shape as git_activity /
    photo-library): a required ``path`` picker (defaults to the two v1 agent
    layouts) plus the first-sync lookback window.
    """
    return ActivationFlowSpec(
        title="Connect coding-assistant history",
        description=(
            "Magi reads ONLY your own messages from your coding-assistant transcripts "
            "(Claude Code, Codex) to learn what you work on. Obvious secrets are scrubbed "
            "first. Content is used to build your profile and may pass through your "
            "configured LLM. Pick the folders to include."
        ),
        confirm_label="Connect",
        cancel_label="Not now",
        enabled_key=f"{prefix}.enabled",
        configured_key=f"{prefix}.initial_sync_configured",
        fields=[
            ExtensionFieldSpec(
                key=f"{prefix}.source_paths",
                type="path",
                label="Transcript Folders",
                description="Folders to scan (e.g. ~/.claude/projects, ~/.codex).",
                default=list(_DEFAULT_SOURCE_PATHS),
                required=True,
                section="activation",
                surface="timeline",
                order=10,
                placeholder="~/.claude/projects",
            ),
            ExtensionFieldSpec(
                key=f"{prefix}.initial_sync_lookback_days",
                type="number",
                label="First-sync window (days)",
                description="First sync ingests sessions from the last N days (raise to import more).",
                default=30,
                min=1,
                max=3650,
                section="activation",
                surface="timeline",
                order=20,
            ),
        ],
    )


class CodingAgentHistoryPlugin(Plugin):
    """Registers the coding-agent history timeline source."""

    def get_sensors(self) -> list[tuple[str, Any, SensorSpec]]:
        sensor = CodingAgentHistorySensor()

        settings: dict[str, Any] = {}
        sensors_settings = self.settings.get("sensors", {})
        if isinstance(sensors_settings, dict):
            settings = dict(sensors_settings.get("coding_agent_history", {}))
        sync_interval_minutes = settings.get(
            "sync_interval_minutes", DEFAULT_SETTINGS["sync_interval_minutes"]
        )

        return [
            (
                "timeline.coding_agent_history",
                sensor,
                SensorSpec(
                    sensor_id="timeline.coding_agent_history",
                    display_name="Coding Agent History",
                    description="Your own turns from AI coding-assistant transcripts (Claude Code, Codex).",
                    domain="timeline",
                    surface="timeline",
                    sync_mode="interval",
                    polling_mode="interval",
                    fields=_fields(_PREFIX),
                    metadata={
                        "source_type": "coding_agent_history",
                        "default_settings": dict(DEFAULT_SETTINGS),
                        "activation_flow": _build_activation_flow(_PREFIX).model_dump(),
                        "sync_interval_minutes": sync_interval_minutes,
                    },
                ),
            )
        ]
