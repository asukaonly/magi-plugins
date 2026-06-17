"""Generic local documents sensor plugin."""
from __future__ import annotations

from typing import Any

from magi_plugin_sdk import (
    ActivationFlowSpec,
    ExtensionFieldOption,
    ExtensionFieldSpec,
    ExtractionProfileSpec,
    Plugin,
    SensorSpec,
    SummaryProfileSpec,
)

from .reader import DEFAULT_EXTENSIONS
from .sensor import DEFAULT_EXCLUDE_FOLDERS, DEFAULT_SEARCH_ONLY_FOLDERS, LocalDocumentsSensor

_PREFIX = "sensors.local_documents"

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


def _fields() -> list[ExtensionFieldSpec]:
    return [
        ExtensionFieldSpec(
            key=f"{_PREFIX}.enabled",
            type="switch",
            label="Enabled",
            description="Whether local document sync is active.",
            default=False,
            section="general",
            surface="timeline",
            order=10,
        ),
        ExtensionFieldSpec(
            key=f"{_PREFIX}.root_paths",
            type="tags",
            label="Document Folders",
            description="Local folders to scan for notes and text documents.",
            default=[],
            required=True,
            section="general",
            surface="timeline",
            order=20,
            placeholder="~/Documents/Notes",
        ),
        ExtensionFieldSpec(
            key=f"{_PREFIX}.sync_mode",
            type="select",
            label="Sync Mode",
            description="How local documents should be synchronized.",
            default="interval",
            required=True,
            options=[
                ExtensionFieldOption(label="Manual", value="manual"),
                ExtensionFieldOption(label="Interval", value="interval"),
            ],
            section="general",
            surface="timeline",
            order=30,
        ),
        ExtensionFieldSpec(
            key=f"{_PREFIX}.sync_interval_minutes",
            type="number",
            label="Sync Interval (minutes)",
            description="How often to rescan the folders for changed documents.",
            default=10,
            min=1,
            max=1440,
            section="general",
            surface="timeline",
            order=40,
            depends_on_key=f"{_PREFIX}.sync_mode",
            depends_on_values=["interval"],
        ),
        ExtensionFieldSpec(
            key=f"{_PREFIX}.include_extensions",
            type="tags",
            label="Included Extensions",
            description="Text file extensions to ingest.",
            default=DEFAULT_EXTENSIONS,
            section="filters",
            surface="timeline",
            order=50,
            placeholder=".md, .txt",
        ),
        ExtensionFieldSpec(
            key=f"{_PREFIX}.exclude_folders",
            type="tags",
            label="Excluded Folders",
            description="Folders never read at all.",
            default=DEFAULT_EXCLUDE_FOLDERS,
            section="privacy",
            surface="timeline",
            order=60,
        ),
        ExtensionFieldSpec(
            key=f"{_PREFIX}.cognition_exclude_folders",
            type="tags",
            label="Search-only Folders",
            description="Folders read for search and timeline but kept out of knowledge extraction.",
            default=DEFAULT_SEARCH_ONLY_FOLDERS,
            section="privacy",
            surface="timeline",
            order=70,
        ),
        ExtensionFieldSpec(
            key=f"{_PREFIX}.max_file_bytes",
            type="number",
            label="Max File Size (bytes)",
            description="Files larger than this are skipped.",
            default=1_000_000,
            min=1024,
            max=20_000_000,
            section="filters",
            surface="timeline",
            order=80,
        ),
        ExtensionFieldSpec(
            key=f"{_PREFIX}.max_body_chars",
            type="number",
            label="Max Text Characters",
            description="Maximum characters stored from each document body.",
            default=50_000,
            min=1000,
            max=500_000,
            section="filters",
            surface="timeline",
            order=90,
        ),
    ]


def _activation_flow() -> ActivationFlowSpec:
    return ActivationFlowSpec(
        title="Enable Local Documents",
        description=(
            "Local documents can contain private notes. Choose the folders Magi may read before enabling this source."
        ),
        confirm_label="Enable source",
        cancel_label="Not now",
        enabled_key=f"{_PREFIX}.enabled",
        configured_key=f"{_PREFIX}.initial_sync_configured",
        fields=[
            ExtensionFieldSpec(
                key=f"{_PREFIX}.root_paths",
                type="tags",
                label="Document Folders",
                description="Local folders to scan for notes and text documents.",
                default=[],
                required=True,
                section="activation",
                surface="timeline",
                order=10,
                placeholder="~/Documents/Notes",
            ),
            ExtensionFieldSpec(
                key=f"{_PREFIX}.cognition_exclude_folders",
                type="tags",
                label="Search-only Folders",
                description="Folders read for search and timeline but kept out of knowledge extraction.",
                default=DEFAULT_SEARCH_ONLY_FOLDERS,
                section="activation",
                surface="timeline",
                order=20,
            ),
        ],
    )


class LocalDocumentsPlugin(Plugin):
    """Registers generic local document sensors."""

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

    def get_sensors(self) -> list[tuple[str, Any, SensorSpec]]:
        sensors_cfg = self.settings.get("sensors", {})
        cfg = dict(sensors_cfg.get("local_documents", {})) if isinstance(sensors_cfg, dict) else {}
        if not bool(cfg.get("enabled", DEFAULT_SETTINGS["enabled"])):
            return []

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

        def _spec(sensor: LocalDocumentsSensor) -> SensorSpec:
            return SensorSpec(
                sensor_id=sensor.sensor_id,
                display_name="Local Documents",
                description="Local notes and text documents ingested into the user timeline.",
                domain="timeline",
                surface="timeline",
                sync_mode=sync_mode,
                polling_mode=getattr(sensor, "polling_mode", "interval"),
                fields=_fields(),
                metadata={
                    "source_type": sensor.source_type,
                    "default_settings": dict(DEFAULT_SETTINGS),
                    "sync_interval_minutes": interval,
                    "activation_flow": _activation_flow().model_dump(),
                },
            )

        result: list[tuple[str, Any, SensorSpec]] = []
        for suffix, cognition in (("knowledge", True), ("search", False)):
            sensor = LocalDocumentsSensor(
                cognition_eligible=cognition,
                sensor_suffix=suffix,
                root_paths=root_paths,
                include_extensions=include_extensions,
                exclude_folders=exclude,
                cognition_exclude_folders=search_only,
                max_file_bytes=max_file_bytes,
                max_body_chars=max_body_chars,
            )
            result.append((sensor.sensor_id, sensor, _spec(sensor)))
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

