# plugin.py
"""Agent history plugin.

Registers pull-sync sources that ingest the user's OWN turns from local agent
transcripts (Claude Code, Codex) so L2 mines them into the user's professional
profile. The authorship crux lives in ``sensor.py`` (``author_type="user"``);
this module wires the per-agent sensors + settings surfaces + first-enable
``activation_flow`` (mirrors git_activity / photo-library).

The sensor is registered unconditionally (not gated on ``enabled``): it reads its
own settings at sync time and returns an empty result when no ``source_paths`` are
configured, so the host always has the spec available to drive the install panel.
"""
from __future__ import annotations

from typing import Any

from magi_plugin_sdk import (
    ActivationFlowSpec,
    ExtensionFieldSpec,
    ExtractionProfileSpec,
    Plugin,
    SensorSpec,
)

from .sensor import CodingAgentHistorySensor

CAPABILITY_ID = "agent_history"
CAPABILITY_DISPLAY_NAME = "Agent History"
CAPABILITY_DESCRIPTION = "Manage local agent transcript history sources."

AGENT_ENTRIES: dict[str, dict[str, Any]] = {
    "claude_code": {
        "source_type": "claude_code_agent_history",
        "display_name": "Claude Code",
        "description": "Your own turns from local Claude Code transcripts.",
        "default_source_paths": ["~/.claude/projects"],
        "order": 10,
    },
    "codex": {
        "source_type": "codex_agent_history",
        "display_name": "Codex",
        "description": "Your own turns from local Codex prompts.",
        "default_source_paths": ["~/.codex"],
        "order": 20,
    },
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "source_paths": [],
    "initial_sync_lookback_days": 30,
    "sync_interval_minutes": 30,
    "initial_sync_configured": False,
}

AGENT_HISTORY_L2_ENTITY_TYPES = [
    "project",
    "product",
    "software",
    "technology",
    "organization",
    "topic",
    "concept",
    "skill",
    "activity",
]

AGENT_HISTORY_L2_PREDICATES = [
    "USES",
    "WORKS_WITH",
    "REFERENCES",
    "INTERESTED_IN",
    "CREATES",
    "PLANS_TO",
]

AGENT_HISTORY_ASSERTION_FAMILIES = [
    "identity_profile",
    "communication_profile",
    "preference_profile",
    "routine_profile",
    "state_profile",
]


def _default_settings_for(entry: dict[str, Any]) -> dict[str, Any]:
    defaults = dict(DEFAULT_SETTINGS)
    defaults["source_paths"] = list(entry["default_source_paths"])
    return defaults


def _fields(prefix: str, entry: dict[str, Any]) -> list[ExtensionFieldSpec]:
    """Ongoing settings surface (shown after the source is enabled)."""
    label = str(entry["display_name"])
    return [
        ExtensionFieldSpec(
            key=f"{prefix}.enabled",
            type="switch",
            label="Enabled",
            description=f"Whether {label} history sync is active.",
            default=False,
            section="general",
            surface="timeline",
            order=10,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.source_paths",
            type="path",
            label="Transcript Folders",
            description=f"Folders to scan for {label} transcripts.",
            default=list(entry["default_source_paths"]),
            required=True,
            section="general",
            surface="timeline",
            order=20,
            placeholder=str(entry["default_source_paths"][0]),
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


def _build_activation_flow(prefix: str, entry: dict[str, Any]) -> ActivationFlowSpec:
    """First-enable consent flow rendered by the host install panel.

    Reuses the shipped activation panel (same shape as git_activity /
    photo-library): a required ``path`` picker plus the first-sync lookback
    window.
    """
    label = str(entry["display_name"])
    return ActivationFlowSpec(
        title=f"Connect {label} history",
        description=(
            f"Magi reads ONLY your own messages from local {label} transcripts "
            "to learn what you work on. Obvious secrets are scrubbed "
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
                description=f"Folders to scan for {label} transcripts.",
                default=list(entry["default_source_paths"]),
                required=True,
                section="activation",
                surface="timeline",
                order=10,
                placeholder=str(entry["default_source_paths"][0]),
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


def _capability_metadata(entry_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "capability_id": CAPABILITY_ID,
        "capability_display_name": CAPABILITY_DISPLAY_NAME,
        "capability_description": CAPABILITY_DESCRIPTION,
        "entry_id": entry_id,
        "entry_display_name": entry["display_name"],
        "entry_description": entry["description"],
        "entry_order": entry["order"],
    }


class CodingAgentHistoryPlugin(Plugin):
    """Registers agent transcript timeline sources."""

    def get_extraction_profiles(self) -> list[ExtractionProfileSpec]:
        return [
            ExtractionProfileSpec(
                profile_id=f"source.{entry['source_type']}",
                source_types=[str(entry["source_type"])],
                allowed_entity_types=AGENT_HISTORY_L2_ENTITY_TYPES,
                allowed_predicates=AGENT_HISTORY_L2_PREDICATES,
                structured_allowed_entity_types=AGENT_HISTORY_L2_ENTITY_TYPES,
                structured_allowed_predicates=AGENT_HISTORY_L2_PREDICATES,
                allowed_assertion_families=AGENT_HISTORY_ASSERTION_FAMILIES,
                allow_graph=True,
                allow_assertion=True,
                assertion_mode="phase2_candidate",
                derived_assertion_specs=[],
                extraction_instructions=(
                    "These events are the user's own turns from local coding-agent transcripts. "
                    "Treat them as user-authored evidence, not assistant output.\n"
                    "Extract durable projects, products, tools, technologies, organizations, "
                    "topics, skills, and work activities. Prefer USES for tools/platforms, "
                    "WORKS_WITH for technologies or project contexts, REFERENCES for mentions, "
                    "INTERESTED_IN only for repeated or explicitly stated interests, CREATES "
                    "for authored projects/artifacts, and PLANS_TO for explicit future work.\n"
                    "Skip secrets, stack traces, pasted logs, file paths, one-off debugging "
                    "details, quoted assistant text, and transient command instructions."
                ),
                phase2_instructions=(
                    "Assertion candidates from coding-agent history must describe stable user "
                    "profile signals from the user's own words: identity/work context, preferred "
                    "communication style, durable interests, recurring workflows, routines, or "
                    "current state. Do not promote temporary requests, pasted errors, external "
                    "docs, or assistant suggestions into long-term profile assertions."
                ),
            )
            for entry in AGENT_ENTRIES.values()
        ]

    def get_sensors(self) -> list[tuple[str, Any, SensorSpec]]:
        sensors_settings = self.settings.get("sensors", {})
        sensors_payload = sensors_settings if isinstance(sensors_settings, dict) else {}
        registered: list[tuple[str, Any, SensorSpec]] = []
        for entry_id, entry in AGENT_ENTRIES.items():
            source_type = str(entry["source_type"])
            settings = dict(sensors_payload.get(source_type, {}))
            defaults = _default_settings_for(entry)
            sync_interval_minutes = settings.get(
                "sync_interval_minutes", defaults["sync_interval_minutes"]
            )
            sensor_id = f"timeline.{source_type}"
            sensor = CodingAgentHistorySensor(
                agent=entry_id,
                source_type=source_type,
                display_name=str(entry["display_name"]),
            )
            prefix = f"sensors.{source_type}"
            registered.append(
                (
                    sensor_id,
                    sensor,
                    SensorSpec(
                        sensor_id=sensor_id,
                        display_name=str(entry["display_name"]),
                        description=str(entry["description"]),
                        domain="timeline",
                        surface="timeline",
                        sync_mode="interval",
                        polling_mode="interval",
                        fields=_fields(prefix, entry),
                        metadata={
                            "source_type": source_type,
                            "default_settings": defaults,
                            "activation_flow": _build_activation_flow(prefix, entry).model_dump(),
                            "sync_interval_minutes": sync_interval_minutes,
                            **_capability_metadata(entry_id, entry),
                        },
                    ),
                )
            )
        return registered
