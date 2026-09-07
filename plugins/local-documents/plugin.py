"""Generic local documents source plugin."""
from __future__ import annotations

from typing import Any

from magi_plugin_sdk import (
    ExtractionProfileSpec,
    Plugin,
    SourceSpec,
    SummaryProfileSpec,
)

from .reader import DEFAULT_EXTENSIONS
from .source import DEFAULT_EXCLUDE_FOLDERS, DEFAULT_SEARCH_ONLY_FOLDERS, LocalDocumentsSource

_PREFIX = "sources.local_documents"
_SOURCE_ENTRY_ID = "local_documents"
_SOURCE_DISPLAY_NAME = "Local Documents"
_SOURCE_DESCRIPTION = "Local notes and text documents ingested into the user timeline."

DEFAULT_SETTINGS = {
    "enabled": False,
    "root_paths": [],
    "include_extensions": DEFAULT_EXTENSIONS,
    "exclude_folders": DEFAULT_EXCLUDE_FOLDERS,
    "cognition_exclude_folders": DEFAULT_SEARCH_ONLY_FOLDERS,
    "max_file_bytes": 1_000_000,
    "max_body_chars": 50_000,
    "sync_mode": "interval",
    "sync_interval_minutes": 10,
    "initial_sync_configured": False,
}


class LocalDocumentsPlugin(Plugin):
    """Registers generic local document sources."""

    def get_extraction_profiles(self) -> list[ExtractionProfileSpec]:
        return [
            ExtractionProfileSpec(
                profile_id="source.local_documents",
                source_types=["local_documents"],
                allowed_entity_types=[
                    "concept",
                    "topic",
                    "person",
                    "project",
                    "software",
                    "organization",
                    "product",
                    "technology",
                    "media",
                ],
                allowed_predicates=[
                    "REFERENCES",
                    "INTERESTED_IN",
                    "KNOWS",
                    "USES",
                    "WORKS_WITH",
                    "MEMBER_OF",
                ],
                structured_allowed_entity_types=["concept", "topic"],
                structured_allowed_predicates=["REFERENCES"],
                allow_graph=True,
                allow_assertion=False,
                extraction_instructions=(
                    "These events are user-authored local notes or text documents.\n"
                    "- Markdown-style [[wikilinks]] and #tags are emitted as deterministic REFERENCES edges.\n"
                    "- From the document body, extract only entities and relations the user clearly wrote about.\n"
                    "- Do not treat quoted, clipped, or reference material as the user's own belief unless the text says so."
                ),
            )
        ]

    def get_sources(self) -> list[tuple[str, Any, SourceSpec]]:
        sources_cfg = self.settings.get("sources", {})
        cfg = dict(sources_cfg.get("local_documents", {})) if isinstance(sources_cfg, dict) else {}
        root_paths = list(cfg.get("root_paths", DEFAULT_SETTINGS["root_paths"]) or [])
        include_extensions = list(cfg.get("include_extensions", DEFAULT_SETTINGS["include_extensions"]) or [])
        exclude = list(cfg.get("exclude_folders", DEFAULT_SETTINGS["exclude_folders"]) or [])
        search_only = list(
            cfg.get("cognition_exclude_folders", DEFAULT_SETTINGS["cognition_exclude_folders"]) or []
        )
        max_file_bytes = int(cfg.get("max_file_bytes", DEFAULT_SETTINGS["max_file_bytes"]))
        max_body_chars = int(cfg.get("max_body_chars", DEFAULT_SETTINGS["max_body_chars"]))
        sync_mode = str(cfg.get("sync_mode", DEFAULT_SETTINGS["sync_mode"]))
        interval = int(cfg.get("sync_interval_minutes", DEFAULT_SETTINGS["sync_interval_minutes"]))

        def _spec(source: LocalDocumentsSource) -> SourceSpec:
            return SourceSpec(
                source_id=source.source_id,
                display_name=_SOURCE_DISPLAY_NAME,
                description=_SOURCE_DESCRIPTION,
                domain="timeline",
                surface="timeline",
                sync_mode=sync_mode,
                polling_mode=getattr(source, "polling_mode", "interval"),
                fields=[
                    field.model_copy(deep=True)
                    for field in sorted(self.manifest.settings_fields, key=lambda field: field.order)
                    if field.surface == "timeline" and field.section != "activation"
                ],
                metadata={
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
            source = LocalDocumentsSource(
                cognition_eligible=cognition,
                source_suffix=suffix,
                root_paths=root_paths,
                include_extensions=include_extensions,
                exclude_folders=exclude,
                cognition_exclude_folders=search_only,
                max_file_bytes=max_file_bytes,
                max_body_chars=max_body_chars,
            )
            result.append((source.source_id, source, _spec(source)))
        return result

    def get_summary_profiles(self) -> list[SummaryProfileSpec]:
        return [
            SummaryProfileSpec(
                profile_id="local-documents:document_activity",
                summary_category="document_activity",
                source_types=["local_documents", "local_documents_search"],
                windows=["day", "week"],
                settle_window_seconds=300,
                min_events=2,
                intent_verbs=[
                    "笔记",
                    "写了",
                    "记录",
                    "文档",
                    "notes",
                    "documents",
                    "wrote",
                    "edited",
                    "recorded",
                ],
                prompt_hints={"category": "document_activity"},
            )
        ]
