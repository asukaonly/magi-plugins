"""Shared plugin contract builders for coding-agent transcript sources."""
from __future__ import annotations

from typing import Any

from magi_plugin_sdk import (
    PluginManifest,
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


def build_source_registration(
    plugin_settings: dict[str, Any],
    *,
    manifest: PluginManifest,
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
            fields=[
                field.model_copy(deep=True)
                for field in sorted(manifest.settings_fields, key=lambda field: field.order)
                if field.surface == "timeline" and field.section != "activation"
            ],
            metadata={
                "source_type": source_type,
                "default_settings": {
                    field.key.rsplit(".", 1)[-1]: field.model_copy(deep=True).default
                    for field in manifest.settings_fields
                    if field.key.startswith("sources.") and field.type != "secret"
                },
                "activation_flow": manifest.activation_flow.model_dump() if manifest.activation_flow is not None else None,
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
