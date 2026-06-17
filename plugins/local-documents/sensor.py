"""Generic local documents timeline sensor."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from magi_plugin_sdk.sensors import (
    ContentBlock,
    SensorBase,
    SensorMemoryPolicy,
    SensorOutput,
    SensorOutputMetadata,
    SensorSyncContext,
    SensorSyncResult,
)

from .reader import (
    DEFAULT_EXTENSIONS,
    classify_folder,
    document_id_for_path,
    normalize_extensions,
    parse_document,
    walk_documents,
)

DEFAULT_EXCLUDE_FOLDERS = [
    ".git",
    ".hg",
    ".svn",
    ".DS_Store",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "node_modules",
    ".venv",
    "venv",
]
DEFAULT_SEARCH_ONLY_FOLDERS = ["References", "Archive", "Clippings"]
_SUMMARY_MAX_CHARS = 280


def _lean_summary(body: str, *, max_chars: int = _SUMMARY_MAX_CHARS) -> str:
    for line in str(body or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped if len(stripped) <= max_chars else stripped[:max_chars].rstrip() + "..."
    return ""


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


class LocalDocumentsSensor(SensorBase):
    """Pull-sync sensor that ingests generic local text documents."""

    source_type = "local_documents"
    polling_mode = "interval"
    default_interval = 10
    update_key_fields = ("source_item_id",)
    relation_edge_whitelist = ()
    supports_pull_sync = True

    def __init__(
        self,
        *,
        cognition_eligible: bool,
        sensor_suffix: str,
        root_paths: Optional[list[str]] = None,
        include_extensions: Optional[list[str]] = None,
        exclude_folders: Optional[list[str]] = None,
        cognition_exclude_folders: Optional[list[str]] = None,
        max_file_bytes: int = 1_000_000,
        max_body_chars: int = 50_000,
    ) -> None:
        super().__init__()
        self.sensor_id = f"timeline.local_documents.{sensor_suffix}"
        self.source_type = "local_documents" if sensor_suffix == "knowledge" else "local_documents_search"
        self.display_name = "Local Documents"
        self._tier = sensor_suffix
        self._root_paths = list(root_paths or [])
        self._include_extensions = normalize_extensions(include_extensions or DEFAULT_EXTENSIONS)
        self._exclude_folders = list(exclude_folders or DEFAULT_EXCLUDE_FOLDERS)
        self._cognition_exclude_folders = list(cognition_exclude_folders or DEFAULT_SEARCH_ONLY_FOLDERS)
        self._max_file_bytes = int(max_file_bytes)
        self._max_body_chars = int(max_body_chars)
        self.memory_policy = SensorMemoryPolicy(
            memory_domain="user_authored",
            ingest_target="l1_only",
            cognition_eligible=bool(cognition_eligible),
            retention_class="permanent",
            importance_bias=0.6,
            author_type="user",
            content_type="text",
        )

    def source_item_identity(self, item: dict[str, Any]) -> str:
        item_id = str(item.get("source_item_id") or "").strip()
        if item_id:
            return item_id
        path = str(item.get("path") or "").strip()
        return document_id_for_path(Path(path)) if path else str(item.get("rel_path") or "").strip()

    async def extract_metadata(self, item: dict[str, Any]) -> SensorOutputMetadata:
        title = str(item.get("title") or "").strip() or str(item.get("rel_path") or "")
        wikilinks = [str(link) for link in (item.get("wikilinks") or []) if str(link).strip()]
        tags = [str(tag) for tag in (item.get("tags") or []) if str(tag).strip()]
        document_ref = f"concept:{title}"

        entities: list[dict[str, Any]] = [
            {"mention_text": title, "entity_type": "concept", "canonical_name_hint": title}
        ]
        for link in wikilinks:
            entities.append(
                {"mention_text": link, "entity_type": "concept", "canonical_name_hint": link}
            )
        for tag in tags:
            entities.append(
                {"mention_text": tag, "entity_type": "topic", "canonical_name_hint": tag}
            )

        def _edge(object_ref: str, object_type: str) -> dict[str, Any]:
            return {
                "subject_ref": document_ref,
                "subject_type": "concept",
                "predicate": "REFERENCES",
                "object_ref": object_ref,
                "object_type": object_type,
                "fact_kind": "explicit_fact",
                "origin_mode": "source_structured",
                "confidence": 0.95,
            }

        fact_hints = [_edge(f"concept:{link}", "concept") for link in wikilinks]
        fact_hints += [_edge(f"topic:{tag}", "topic") for tag in tags]

        return SensorOutputMetadata(
            entities=entities,
            tags=tags,
            fact_hints=fact_hints,
            relation_candidates=[],
        )

    def _resolve_settings(self, context: SensorSyncContext) -> dict[str, Any]:
        sensors = context.plugin_settings.get("sensors", {})
        live = sensors.get("local_documents", {}) if isinstance(sensors, dict) else {}
        return {
            "root_paths": _as_list(live.get("root_paths", self._root_paths)),
            "include_extensions": normalize_extensions(live.get("include_extensions", self._include_extensions)),
            "exclude_folders": _as_list(live.get("exclude_folders", self._exclude_folders)),
            "cognition_exclude_folders": _as_list(
                live.get("cognition_exclude_folders", self._cognition_exclude_folders)
            ),
            "max_file_bytes": int(live.get("max_file_bytes", self._max_file_bytes) or self._max_file_bytes),
            "max_body_chars": int(live.get("max_body_chars", self._max_body_chars) or self._max_body_chars),
        }

    async def collect_items(self, context: SensorSyncContext) -> SensorSyncResult:
        settings = self._resolve_settings(context)
        root_paths = list(settings["root_paths"])
        if not root_paths:
            return SensorSyncResult(
                items=[],
                next_cursor=context.last_cursor,
                watermark_ts=time.time(),
                stats={"count": 0, "error": "no root_paths"},
            )

        try:
            since = float(context.last_cursor) if context.last_cursor else 0.0
        except (TypeError, ValueError):
            since = 0.0

        include_extensions = list(settings["include_extensions"])
        exclude_folders = list(settings["exclude_folders"])
        search_only_folders = list(settings["cognition_exclude_folders"])
        max_file_bytes = int(settings["max_file_bytes"])
        max_body_chars = int(settings["max_body_chars"])
        limit = max(1, int(context.limit or 1000))

        candidates: list[dict[str, Any]] = []
        scanned = 0
        invalid_roots = 0
        skipped_errors = 0
        skipped_oversized = 0

        for raw_root in root_paths:
            root = Path(raw_root).expanduser()
            if not root.is_dir():
                invalid_roots += 1
                continue
            for path in walk_documents(root, include_extensions):
                scanned += 1
                try:
                    rel_path = path.relative_to(root).as_posix()
                    tier = classify_folder(rel_path, exclude_folders, search_only_folders)
                    if tier == "exclude" or tier != self._tier:
                        continue
                    stat = path.stat()
                    if stat.st_size > max_file_bytes:
                        skipped_oversized += 1
                        continue
                    if stat.st_mtime <= since:
                        continue
                    candidates.append(parse_document(path, root, max_body_chars=max_body_chars))
                except (OSError, UnicodeError, ValueError):
                    skipped_errors += 1

        candidates.sort(key=lambda item: float(item.get("mtime") or 0.0))
        items = candidates[:limit]
        max_mtime = max([float(item.get("mtime") or 0.0) for item in items] + [since])
        return SensorSyncResult(
            items=items,
            next_cursor=str(max_mtime) if max_mtime > 0 else context.last_cursor,
            watermark_ts=max_mtime or time.time(),
            stats={
                "count": len(items),
                "scanned": scanned,
                "invalid_roots": invalid_roots,
                "skipped_errors": skipped_errors,
                "skipped_oversized": skipped_oversized,
                "tier": self._tier,
            },
        )

    async def build_output(self, item: dict[str, Any]) -> SensorOutput:
        body = str(item.get("body") or "")
        summary = _lean_summary(body)
        title = str(item.get("title") or "").strip() or None
        tags = [str(tag) for tag in (item.get("tags") or []) if str(tag).strip()]
        wikilinks = [str(link) for link in (item.get("wikilinks") or []) if str(link).strip()]
        mtime = float(item.get("mtime") or time.time())
        extension = str(item.get("extension") or "").strip().lower()

        content_blocks: list[ContentBlock] = [ContentBlock(kind="text", value=summary)]
        content_blocks += [ContentBlock(kind="wikilink", value=link) for link in wikilinks]
        content_blocks += [ContentBlock(kind="tag", value=tag) for tag in tags]

        output = self._build_output(
            source_item_id=self.source_item_identity(item),
            activity=self._build_activity(
                source=self._build_activity_facet(
                    code="local_documents",
                    i18n_key="activity.source.local_documents",
                    fallback="Local Documents",
                    embedding_fallback="local document",
                ),
                action=self._build_activity_facet(
                    code="edited",
                    i18n_key="activity.action.edited",
                    fallback="edited",
                ),
                object=self._build_activity_facet(
                    code="document",
                    i18n_key="activity.object.document",
                    fallback="document",
                ),
                qualifiers={
                    "word_count": len(body.split()),
                    "wikilink_count": len(wikilinks),
                    "tag_count": len(tags),
                    "extension": extension,
                    "truncated": bool(item.get("truncated")),
                },
            ),
            narration=self._build_narration(title=title, body=summary),
            occurred_at=mtime,
            content_blocks=content_blocks,
            tags=tags,
            provenance={
                "sensor_id": self.sensor_id,
                "root_path": item.get("root_path"),
                "rel_path": item.get("rel_path"),
                "extension": extension,
                "document_kind": item.get("document_kind"),
                "size": int(item.get("size") or 0),
            },
            domain_payload={"wikilinks": wikilinks, "tier": self._tier},
        )
        output.pinned_payload = body or None
        return output

