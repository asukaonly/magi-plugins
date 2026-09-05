"""Shared plugin contract builders for coding-agent transcript sources."""
from __future__ import annotations

from typing import Any

from magi_plugin_sdk import (
    ActivationFlowSpec,
    ExtensionFieldSpec,
    ExtractionProfileSpec,
    SourceSpec,
)

from .source import CodingAgentHistorySource

CAPABILITY_ID = "agent_history"
CAPABILITY_DISPLAY_NAME = "Agent History"
CAPABILITY_DESCRIPTION = "Manage installed coding-agent transcript sources."

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
    "interest_profile",
    "project_profile",
    "routine_profile",
    "state_profile",
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "source_paths": [],
    "initial_sync_lookback_days": 30,
    "sync_interval_minutes": 30,
    "initial_sync_configured": False,
}


def source_defaults(default_source_paths: list[str]) -> dict[str, Any]:
    defaults = dict(DEFAULT_SETTINGS)
    defaults["source_paths"] = list(default_source_paths)
    return defaults


def build_extraction_profile(source_type: str) -> ExtractionProfileSpec:
    return ExtractionProfileSpec(
        profile_id=f"source.{source_type}",
        source_types=[source_type],
        allowed_entity_types=AGENT_HISTORY_L2_ENTITY_TYPES,
        allowed_predicates=AGENT_HISTORY_L2_PREDICATES,
        structured_allowed_entity_types=AGENT_HISTORY_L2_ENTITY_TYPES,
        structured_allowed_predicates=AGENT_HISTORY_L2_PREDICATES,
        allowed_assertion_families=AGENT_HISTORY_ASSERTION_FAMILIES,
        allow_graph=True,
        allow_assertion=True,
        derived_assertion_specs=[],
        extraction_instructions=(
            "These events are the user's own turns from local coding-agent transcripts. "
            "Treat them as user-authored evidence, not assistant output.\n"
            "Extract durable projects, products, tools, technologies, organizations, "
            "topics, skills, and work activities. Skip secrets, stack traces, pasted "
            "logs, file paths, one-off debugging details, quoted assistant text, and "
            "transient command instructions."
        ),
    )


def build_fields(
    prefix: str,
    *,
    display_name: str,
    default_source_paths: list[str],
) -> list[ExtensionFieldSpec]:
    return [
        ExtensionFieldSpec(
            key=f"{prefix}.enabled",
            type="switch",
            label="Enabled",
            description=f"Whether {display_name} history sync is active.",
            default=False,
            section="general",
            surface="timeline",
            order=10,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.source_paths",
            type="path",
            label="Transcript Folders",
            description=f"Folders to scan for {display_name} transcripts.",
            default=list(default_source_paths),
            required=True,
            section="general",
            surface="timeline",
            order=20,
            placeholder=default_source_paths[0],
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.initial_sync_lookback_days",
            type="number",
            label="First-sync window (days)",
            description="First sync ingests sessions from the last N days.",
            default=30,
            minimum=1,
            maximum=3650,
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
            minimum=5,
            maximum=1440,
            section="general",
            surface="timeline",
            order=40,
        ),
    ]


def build_activation_flow(
    prefix: str,
    *,
    display_name: str,
    default_source_paths: list[str],
) -> ActivationFlowSpec:
    return ActivationFlowSpec(
        title=f"Connect {display_name} history",
        description=(
            f"Magi reads only your own messages from local {display_name} "
            "transcripts. Obvious secrets are scrubbed first."
        ),
        confirm_label="Connect",
        cancel_label="Not now",
        enabled_key=f"{prefix}.enabled",
        configured_key=f"{prefix}.initial_sync_configured",
        first_context={
            "max_items_per_sync": 200,
            "settings_overrides": {
                f"{prefix}.initial_sync_lookback_days": 30,
            },
        },
        fields=[
            ExtensionFieldSpec(
                key=f"{prefix}.source_paths",
                type="path",
                label="Transcript Folders",
                description=f"Folders to scan for {display_name} transcripts.",
                default=list(default_source_paths),
                required=True,
                section="activation",
                surface="timeline",
                order=10,
                placeholder=default_source_paths[0],
            ),
            ExtensionFieldSpec(
                key=f"{prefix}.initial_sync_lookback_days",
                type="number",
                label="First-sync window (days)",
                description="First sync ingests sessions from the last N days.",
                default=30,
                minimum=1,
                maximum=3650,
                section="activation",
                surface="timeline",
                order=20,
            ),
        ],
    )


def build_source_registration(
    plugin_settings: dict[str, Any],
    *,
    agent: str,
    source_type: str,
    display_name: str,
    description: str,
    default_source_paths: list[str],
    entry_order: int,
) -> tuple[str, Any, SourceSpec]:
    sources_settings = plugin_settings.get("sources", {})
    sources_payload = sources_settings if isinstance(sources_settings, dict) else {}
    settings = dict(sources_payload.get(source_type, {}))
    defaults = source_defaults(default_source_paths)
    sync_interval_minutes = settings.get(
        "sync_interval_minutes", defaults["sync_interval_minutes"]
    )
    source_id = f"timeline.{source_type}"
    source = CodingAgentHistorySource(
        agent=agent,
        source_type=source_type,
        display_name=display_name,
        default_source_paths=list(default_source_paths),
    )
    prefix = f"sources.{source_type}"
    return (
        source_id,
        source,
        SourceSpec(
            source_id=source_id,
            display_name=display_name,
            description=description,
            domain="timeline",
            surface="timeline",
            sync_mode="interval",
            polling_mode="interval",
            fields=build_fields(
                prefix,
                display_name=display_name,
                default_source_paths=default_source_paths,
            ),
            metadata={
                "source_type": source_type,
                "default_settings": defaults,
                "activation_flow": build_activation_flow(
                    prefix,
                    display_name=display_name,
                    default_source_paths=default_source_paths,
                ).model_dump(),
                "sync_interval_minutes": sync_interval_minutes,
                "capability_id": CAPABILITY_ID,
                "capability_display_name": CAPABILITY_DISPLAY_NAME,
                "capability_description": CAPABILITY_DESCRIPTION,
                "entry_id": agent,
                "entry_display_name": display_name,
                "entry_description": description,
                "entry_order": entry_order,
            },
        ),
    )
