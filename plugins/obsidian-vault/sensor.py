# sensor.py
"""Obsidian vault timeline sensor."""
from __future__ import annotations

import time
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

from .reader import classify_folder, parse_note, walk_markdown
from pathlib import Path


class ObsidianVaultSensor(SensorBase):
    """Pull-sync sensor that ingests Obsidian markdown notes.

    Instantiated twice by the plugin: a ``knowledge`` instance
    (``cognition_eligible=True``) and a ``search`` instance
    (``cognition_eligible=False``). ``SensorMemoryPolicy`` is per-instance, which is
    how the spec's folder-based cognition gating (Option X) is realized.
    """

    source_type = "obsidian_vault"
    polling_mode = "interval"
    default_interval = 10  # minutes
    update_key_fields = ("source_item_id",)
    relation_edge_whitelist = ("REFERENCES", "TAGGED_AS")
    supports_pull_sync = True

    def __init__(
        self,
        *,
        cognition_eligible: bool,
        sensor_suffix: str,
        vault_path: Optional[str] = None,
        exclude_folders: Optional[list[str]] = None,
        cognition_exclude_folders: Optional[list[str]] = None,
    ) -> None:
        super().__init__()
        self.sensor_id = f"timeline.obsidian_vault.{sensor_suffix}"
        self.display_name = "Obsidian Vault"
        self._tier = sensor_suffix
        self._vault_path = vault_path
        self._exclude_folders = exclude_folders or []
        self._cognition_exclude_folders = cognition_exclude_folders or []
        self.memory_policy = SensorMemoryPolicy(
            memory_domain="user_authored",
            ingest_target="l1_only",
            cognition_eligible=bool(cognition_eligible),
            retention_class="permanent",
            importance_bias=0.6,
            author_type="user",
            content_type="text",
        )

    def source_item_identity(self, item: dict) -> str:
        """Stable id for supersession: frontmatter uid if present, else vault-relative path."""
        uid = str(item.get("uid") or "").strip()
        return uid or str(item.get("rel_path") or "").strip()

    async def extract_metadata(self, item: dict[str, Any]) -> SensorOutputMetadata:
        """Pre-extract high-confidence structured signals from the note.

        wikilinks -> note entities + REFERENCES relation candidates;
        tags      -> tag list + TAGGED_AS relation candidates.
        These are unambiguous, so they are emitted even though free-prose extraction
        only runs for the knowledge (cognition_eligible) instance.
        """
        title = str(item.get("title") or "").strip()
        note_surface = title or str(item.get("rel_path") or "")
        wikilinks = [str(w) for w in (item.get("wikilinks") or []) if str(w).strip()]
        tags = [str(t) for t in (item.get("tags") or []) if str(t).strip()]

        entities: list[dict[str, Any]] = [
            {
                "surface": note_surface,
                "normalized_name": note_surface,
                "entity_type": "note",
                "alias_signals": list(item.get("aliases") or []),
            }
        ]
        for link in wikilinks:
            entities.append({"surface": link, "normalized_name": link, "entity_type": "note"})

        relation_candidates: list[dict[str, Any]] = []
        for link in wikilinks:
            relation_candidates.append({
                "subject_ref": note_surface,
                "subject_type": "note",
                "predicate": "REFERENCES",
                "object_ref": link,
                "object_type": "note",
                "confidence": 0.95,
            })
        for tag in tags:
            relation_candidates.append({
                "subject_ref": note_surface,
                "subject_type": "note",
                "predicate": "TAGGED_AS",
                "object_ref": tag,
                "object_type": "topic",
                "confidence": 0.95,
            })

        return SensorOutputMetadata(
            entities=entities,
            tags=tags,
            fact_hints=[],
            relation_candidates=relation_candidates,
        )

    def _resolve_settings(self, context: SensorSyncContext) -> dict[str, Any]:
        """Merge constructor defaults with live plugin settings for this sensor."""
        sensors = context.plugin_settings.get("sensors", {})
        live = sensors.get("obsidian_vault", {}) if isinstance(sensors, dict) else {}
        return {
            "vault_path": live.get("vault_path", self._vault_path) or "",
            "exclude_folders": live.get("exclude_folders", self._exclude_folders) or [],
            "cognition_exclude_folders": live.get(
                "cognition_exclude_folders", self._cognition_exclude_folders
            ) or [],
        }

    async def collect_items(self, context: SensorSyncContext) -> SensorSyncResult:
        settings = self._resolve_settings(context)
        vault_path = str(settings["vault_path"]).strip()
        if not vault_path:
            return SensorSyncResult(items=[], next_cursor=context.last_cursor,
                                    watermark_ts=time.time(),
                                    stats={"count": 0, "error": "no vault_path"})

        vault_root = Path(vault_path).expanduser()
        if not vault_root.is_dir():
            return SensorSyncResult(items=[], next_cursor=context.last_cursor,
                                    watermark_ts=time.time(),
                                    stats={"count": 0, "error": "vault_path not a directory"})

        try:
            since = float(context.last_cursor) if context.last_cursor else 0.0
        except (TypeError, ValueError):
            since = 0.0

        exclude = list(settings["exclude_folders"])
        search_only = list(settings["cognition_exclude_folders"])

        items: list[dict[str, Any]] = []
        max_mtime = since
        scanned = 0
        for path in walk_markdown(vault_root):
            rel = path.relative_to(vault_root).as_posix()
            tier = classify_folder(rel, exclude, search_only)
            if tier == "exclude":
                continue
            if tier != self._tier:
                continue  # this instance only handles its own tier
            mtime = path.stat().st_mtime
            if mtime <= since:
                continue
            scanned += 1
            note = parse_note(path, vault_root)
            note["source_item_id"] = self.source_item_identity(note)
            items.append(note)
            if mtime > max_mtime:
                max_mtime = mtime
            if len(items) >= int(context.limit or 1000):
                break

        items.sort(key=lambda it: float(it.get("mtime") or 0.0), reverse=True)
        return SensorSyncResult(
            items=items,
            next_cursor=str(max_mtime) if max_mtime > 0 else context.last_cursor,
            watermark_ts=max_mtime or time.time(),
            stats={"count": len(items), "tier": self._tier},
        )

    async def build_output(self, item: dict) -> SensorOutput:
        body = str(item.get("body") or "")
        title = str(item.get("title") or "").strip() or None
        tags = [str(t) for t in (item.get("tags") or []) if str(t).strip()]
        wikilinks = [str(w) for w in (item.get("wikilinks") or []) if str(w).strip()]
        mtime = float(item.get("mtime") or time.time())

        content_blocks: list[ContentBlock] = [ContentBlock(kind="text", value=body)]
        content_blocks += [ContentBlock(kind="wikilink", value=w) for w in wikilinks]
        content_blocks += [ContentBlock(kind="tag", value=t) for t in tags]

        return self._build_output(
            source_item_id=self.source_item_identity(item),
            activity=self._build_activity(
                source=self._build_activity_facet(
                    code="obsidian",
                    i18n_key="activity.source.obsidian",
                    fallback="Obsidian",
                    embedding_fallback="Obsidian note",
                ),
                action=self._build_activity_facet(
                    code="edited",
                    i18n_key="activity.action.edited",
                    fallback="edited",
                ),
                object=self._build_activity_facet(
                    code="note",
                    i18n_key="activity.object.note",
                    fallback="note",
                ),
                qualifiers={
                    "word_count": len(body.split()),
                    "wikilink_count": len(wikilinks),
                    "tag_count": len(tags),
                },
            ),
            narration=self._build_narration(title=title, body=body),
            occurred_at=mtime,
            content_blocks=content_blocks,
            tags=tags,
            provenance={
                "sensor_id": self.sensor_id,
                "rel_path": item.get("rel_path"),
                "aliases": item.get("aliases") or [],
            },
            domain_payload={"wikilinks": wikilinks, "tier": self._tier},
        )
