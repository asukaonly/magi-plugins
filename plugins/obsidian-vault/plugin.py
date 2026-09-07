# plugin.py
"""Obsidian Vault source plugin."""
from __future__ import annotations

from typing import Any

from magi_plugin_sdk import (
    ExtractionProfileSpec,
    Plugin,
    SourceSpec,
)

from .source import ObsidianVaultSource

_PREFIX = "sources.obsidian_vault"
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


class ObsidianVaultPlugin(Plugin):
    """Registers two Obsidian vault sources (knowledge + search-only)."""

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

    def get_sources(self) -> list[tuple[str, Any, SourceSpec]]:
        sources_cfg = self.settings.get("sources", {})
        cfg = dict(sources_cfg.get("obsidian_vault", {})) if isinstance(sources_cfg, dict) else {}
        vault_path = str(cfg.get("vault_path", "")).strip()
        exclude = cfg.get("exclude_folders", DEFAULT_SETTINGS["exclude_folders"])
        search_only = cfg.get("cognition_exclude_folders", DEFAULT_SETTINGS["cognition_exclude_folders"])
        interval = cfg.get("sync_interval_minutes", DEFAULT_SETTINGS["sync_interval_minutes"])

        def _spec(source: ObsidianVaultSource) -> SourceSpec:
            return SourceSpec(
                source_id=source.source_id,
                display_name=_SOURCE_DISPLAY_NAME,
                description=_SOURCE_DESCRIPTION,
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
                    # Per-tier source_type so the two sources get independent
                    # registry/schedule/cursor entries (no first-match-wins collision).
                    "source_type": source.source_type,
                    "default_settings": {
                        field.key.rsplit(".", 1)[-1]: field.model_copy(deep=True).default
                        for field in self.manifest.settings_fields
                        if field.key.startswith("sources.") and field.type != "secret"
                    },
                    "sync_interval_minutes": interval,
                    "activation_flow": self.manifest.activation_flow.model_dump() if self.manifest.activation_flow is not None else None,
                    "capability_id": _SOURCE_ENTRY_ID,
                    "capability_display_name": _SOURCE_DISPLAY_NAME,
                    "capability_description": _SOURCE_DESCRIPTION,
                    "entry_id": _SOURCE_ENTRY_ID,
                    "entry_display_name": _SOURCE_DISPLAY_NAME,
                    "entry_description": _SOURCE_DESCRIPTION,
                },
            )

        result: list[tuple[str, Any, SourceSpec]] = []
        for suffix, cognition in (("knowledge", True), ("search", False)):
            source = ObsidianVaultSource(
                cognition_eligible=cognition,
                source_suffix=suffix,
                vault_path=vault_path,
                exclude_folders=list(exclude),
                cognition_exclude_folders=list(search_only),
            )
            result.append((source.source_id, source, _spec(source)))
        return result
