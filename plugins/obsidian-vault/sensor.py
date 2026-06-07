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
