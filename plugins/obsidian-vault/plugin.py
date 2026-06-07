# plugin.py
"""Obsidian Vault sensor plugin."""
from __future__ import annotations

from typing import Any

from magi_plugin_sdk import (
    ExtensionFieldSpec,
    ExtractionProfileSpec,
    Plugin,
    SensorSpec,
)

from .sensor import ObsidianVaultSensor

_PREFIX = "sensors.obsidian_vault"

DEFAULT_SETTINGS = {
    "enabled": False,
    "vault_path": "",
    "exclude_folders": [".obsidian", ".trash", "Templates"],
    "cognition_exclude_folders": ["Clippings", "References"],
    "sync_interval_minutes": 10,
    "initial_sync_configured": False,
}


def _fields() -> list[ExtensionFieldSpec]:
    return [
        ExtensionFieldSpec(
            key=f"{_PREFIX}.enabled", type="switch", label="Enabled",
            description="Whether the Obsidian vault sensor is active.",
            default=False, section="general", surface="timeline", order=10,
        ),
        ExtensionFieldSpec(
            key=f"{_PREFIX}.vault_path", type="path", label="Vault Folder",
            description="Path to your Obsidian vault.",
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
            default=10, section="general", surface="timeline", order=50,
        ),
    ]


class ObsidianVaultPlugin(Plugin):
    """Registers two Obsidian vault sensors (knowledge + search-only)."""

    def get_extraction_profiles(self) -> list[ExtractionProfileSpec]:
        return [
            ExtractionProfileSpec(
                profile_id="source.obsidian_vault",
                source_types=["obsidian_vault"],
                allowed_entity_types=["note", "person", "topic", "concept"],
                allowed_predicates=["REFERENCES", "TAGGED_AS", "MENTIONS"],
                structured_allowed_entity_types=["note", "topic"],
                structured_allowed_predicates=["REFERENCES", "TAGGED_AS"],
                allow_graph=True,
                allow_assertion=True,
                extraction_instructions=(
                    "These events are user-authored Obsidian notes.\n"
                    "- Treat [[wikilinks]] as REFERENCES edges between notes/entities.\n"
                    "- Treat #tags as TAGGED_AS topics.\n"
                    "- Extract entities the user clearly writes about (people, projects,\n"
                    "  concepts). Do NOT assert quoted or third-party claims as the user's\n"
                    "  own beliefs."
                ),
            )
        ]

    def get_sensors(self) -> list[tuple[str, Any, SensorSpec]]:
        sensors_cfg = self.settings.get("sensors", {})
        cfg = dict(sensors_cfg.get("obsidian_vault", {})) if isinstance(sensors_cfg, dict) else {}
        if not bool(cfg.get("enabled", DEFAULT_SETTINGS["enabled"])):
            return []

        vault_path = str(cfg.get("vault_path", "")).strip()
        exclude = cfg.get("exclude_folders", DEFAULT_SETTINGS["exclude_folders"])
        search_only = cfg.get("cognition_exclude_folders", DEFAULT_SETTINGS["cognition_exclude_folders"])
        interval = cfg.get("sync_interval_minutes", DEFAULT_SETTINGS["sync_interval_minutes"])

        def _spec(sensor_id: str) -> SensorSpec:
            return SensorSpec(
                sensor_id=sensor_id,
                display_name="Obsidian Vault",
                description="Obsidian vault note ingestion for the timeline.",
                domain="timeline",
                surface="timeline",
                sync_mode="interval",
                polling_mode="interval",
                fields=_fields(),
                metadata={
                    "source_type": "obsidian_vault",
                    "default_settings": dict(DEFAULT_SETTINGS),
                    "sync_interval_minutes": interval,
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
            result.append((sensor.sensor_id, sensor, _spec(sensor.sensor_id)))
        return result
