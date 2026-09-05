# plugin.py
"""Obsidian Vault sensor plugin."""
from __future__ import annotations

from typing import Any

from magi_plugin_sdk import (
    ActivationFlowSpec,
    ExtensionFieldSpec,
    ExtractionProfileSpec,
    Plugin,
    SensorSpec,
)

from .sensor import ObsidianVaultSensor

_PREFIX = "sensors.obsidian_vault"
_SOURCE_ENTRY_ID = "obsidian_vault"
_SOURCE_DISPLAY_NAME = "Obsidian Vault"
_SOURCE_DESCRIPTION = "Obsidian vault note ingestion for the timeline."

DEFAULT_SETTINGS = {
    "enabled": False,
    "vault_path": "",
    "exclude_folders": [".obsidian", ".trash", "Templates"],
    "cognition_exclude_folders": ["Clippings", "References"],
    "sync_interval_minutes": 10,
    "initial_sync_configured": False,
}


def _activation_flow() -> ActivationFlowSpec:
    return ActivationFlowSpec(
        title="Enable Obsidian Vault",
        description="Choose the Obsidian vault Magi may read before enabling this source.",
        confirm_label="Enable source",
        cancel_label="Not now",
        enabled_key=f"{_PREFIX}.enabled",
        configured_key=f"{_PREFIX}.initial_sync_configured",
        first_context={"max_items_per_sync": 200},
        fields=[
            ExtensionFieldSpec(
                key=f"{_PREFIX}.vault_path",
                type="path",
                path_kind="directory",
                label="Obsidian Vault Folder",
                description=(
                    "Choose the vault root folder that contains the .obsidian directory."
                ),
                default="",
                required=True,
                section="activation",
                surface="timeline",
                order=10,
            ),
            ExtensionFieldSpec(
                key=f"{_PREFIX}.cognition_exclude_folders",
                type="tags",
                label="Search-only Folders",
                description="Folders read for search but kept out of knowledge extraction.",
                default=["Clippings", "References"],
                section="activation",
                surface="timeline",
                order=20,
            ),
        ],
    )


def _fields() -> list[ExtensionFieldSpec]:
    return [
        ExtensionFieldSpec(
            key=f"{_PREFIX}.enabled", type="switch", label="Enabled",
            description="Whether the Obsidian vault sensor is active.",
            default=False, section="general", surface="timeline", order=10,
        ),
        ExtensionFieldSpec(
            key=f"{_PREFIX}.vault_path", type="path", path_kind="directory",
            label="Obsidian Vault Folder",
            description="Choose the vault root folder that contains the .obsidian directory.",
            default="", required=True, section="general", surface="timeline", order=20,
        ),
        ExtensionFieldSpec(
            key=f"{_PREFIX}.exclude_folders", type="tags", label="Excluded Folders",
            description="Folders never read at all (privacy). Defaults skip Obsidian internals.",
            default=[".obsidian", ".trash", "Templates"],
            section="privacy", surface="timeline", order=30,
        ),
        ExtensionFieldSpec(
            key=f"{_PREFIX}.cognition_exclude_folders", type="tags",
            label="Search-only Folders",
            description="Folders read for search but kept out of the knowledge graph "
                        "(e.g. clippings, references).",
            default=["Clippings", "References"],
            section="privacy", surface="timeline", order=40,
        ),
        ExtensionFieldSpec(
            key=f"{_PREFIX}.sync_interval_minutes", type="number",
            label="Sync Interval (minutes)",
            description="How often to rescan the vault for changes.",
            default=10, minimum=1, maximum=1440, section="general", surface="timeline", order=50,
        ),
    ]


class ObsidianVaultPlugin(Plugin):
    """Registers two Obsidian vault sensors (knowledge + search-only)."""

    def get_extraction_profiles(self) -> list[ExtractionProfileSpec]:
        return [
            ExtractionProfileSpec(
                profile_id="source.obsidian_vault",
                source_types=["obsidian_vault"],
                # Every type/predicate MUST be in the host ENTITY_TYPE_REGISTRY /
                # PREDICATE_REGISTRY or the whole profile is rejected at load. Notes and
                # wikilink targets map to `concept`; tags to `topic`.
                allowed_entity_types=["concept", "topic", "person", "project", "software", "organization"],
                allowed_predicates=["REFERENCES", "INTERESTED_IN", "KNOWS", "USES", "WORKS_WITH", "MEMBER_OF"],
                structured_allowed_entity_types=["concept", "topic"],
                structured_allowed_predicates=["REFERENCES"],
                allow_graph=True,
                allow_assertion=False,
                extraction_instructions=(
                    "These events are user-authored Obsidian notes.\n"
                    "- [[wikilinks]] and #tags are already emitted as deterministic REFERENCES\n"
                    "  edges (structured hints); do not re-derive them.\n"
                    "- From the note body, extract entities the user clearly writes about\n"
                    "  (people, projects, concepts) and relations among them.\n"
                    "- Do NOT assert quoted, clipped, or third-party claims as the user's own\n"
                    "  beliefs."
                ),
            )
        ]

    def get_sensors(self) -> list[tuple[str, Any, SensorSpec]]:
        sensors_cfg = self.settings.get("sensors", {})
        cfg = dict(sensors_cfg.get("obsidian_vault", {})) if isinstance(sensors_cfg, dict) else {}
        vault_path = str(cfg.get("vault_path", "")).strip()
        exclude = cfg.get("exclude_folders", DEFAULT_SETTINGS["exclude_folders"])
        search_only = cfg.get("cognition_exclude_folders", DEFAULT_SETTINGS["cognition_exclude_folders"])
        interval = cfg.get("sync_interval_minutes", DEFAULT_SETTINGS["sync_interval_minutes"])

        def _spec(sensor: ObsidianVaultSensor) -> SensorSpec:
            return SensorSpec(
                sensor_id=sensor.sensor_id,
                display_name=_SOURCE_DISPLAY_NAME,
                description=_SOURCE_DESCRIPTION,
                domain="timeline",
                surface="timeline",
                sync_mode="interval",
                polling_mode="interval",
                fields=_fields(),
                metadata={
                    # Per-tier source_type so the two sensors get independent
                    # registry/schedule/cursor entries (no first-match-wins collision).
                    "source_type": sensor.source_type,
                    "default_settings": dict(DEFAULT_SETTINGS),
                    "sync_interval_minutes": interval,
                    "activation_flow": _activation_flow().model_dump(),
                    "capability_id": _SOURCE_ENTRY_ID,
                    "capability_display_name": _SOURCE_DISPLAY_NAME,
                    "capability_description": _SOURCE_DESCRIPTION,
                    "entry_id": _SOURCE_ENTRY_ID,
                    "entry_display_name": _SOURCE_DISPLAY_NAME,
                    "entry_description": _SOURCE_DESCRIPTION,
                },
            )

        result: list[tuple[str, Any, SensorSpec]] = []
        for suffix, cognition in (("knowledge", True), ("search", False)):
            sensor = ObsidianVaultSensor(
                cognition_eligible=cognition,
                sensor_suffix=suffix,
                vault_path=vault_path,
                exclude_folders=list(exclude),
                cognition_exclude_folders=list(search_only),
            )
            result.append((sensor.sensor_id, sensor, _spec(sensor)))
        return result
