# sensor.py
"""Obsidian vault timeline sensor."""
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

from .reader import classify_folder, parse_note, walk_markdown

_SUMMARY_MAX_CHARS = 280


def _lean_summary(body: str, *, max_chars: int = _SUMMARY_MAX_CHARS) -> str:
    """First non-empty line of the note, capped — a lean L1 preview (RFC #56 P3).

    The full note body is pinned separately for L2; L1 only needs a short,
    timeline-friendly summary, not the whole document.
    """
    for line in str(body or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped if len(stripped) <= max_chars else stripped[:max_chars].rstrip() + "…"
    return ""


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
    # Unused by the host (it reads L2 fact_hints, not the timeline relation_candidates
    # whitelist) — kept empty to avoid implying a route we don't use.
    relation_edge_whitelist = ()
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
        # Distinct source_type per tier. The host resolves/schedules/cursors sensors by
        # (plugin_id, source_type) and resolve_source_sensor() is first-match-wins, so two
        # sensors sharing a source_type collide (only the first ever runs). Distinct
        # source_types give each tier its own schedule + cursor.
        self.source_type = (
            "obsidian_vault" if sensor_suffix == "knowledge" else f"obsidian_vault_{sensor_suffix}"
        )
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
        """Pre-extract high-confidence, source-owned structured signals.

        Emitted in the exact shape the L2 host consumes:
          - entity hints (``entities``): ``mention_text`` / ``entity_type`` / ``canonical_name_hint``
            (the L2 entity-hint reader skips items without ``mention_text``).
          - graph hints (``fact_hints``): typed ``REFERENCES`` edges with
            ``fact_kind="explicit_fact"`` + ``origin_mode="source_structured"`` so they are
            written deterministically (no LLM). ``*_ref`` use ``type:name`` form so the host
            auto-registers catalog entities. (Projection routes ``fact_hints`` ->
            ``structured_graph_hints``; ``relation_candidates`` is a different, timeline-only path.)

        Notes map to the ``concept`` entity type (the ontology has no ``note`` type);
        wikilink targets are ``concept``; tags are ``topic``. Both wikilinks and tags become
        ``REFERENCES`` edges from the note — a tag means the note *references* that topic,
        which avoids over-claiming user interest.
        """
        title = str(item.get("title") or "").strip()
        note_name = title or str(item.get("rel_path") or "")
        wikilinks = [str(w) for w in (item.get("wikilinks") or []) if str(w).strip()]
        tags = [str(t) for t in (item.get("tags") or []) if str(t).strip()]
        note_ref = f"concept:{note_name}"

        entities: list[dict[str, Any]] = [
            {"mention_text": note_name, "entity_type": "concept", "canonical_name_hint": note_name}
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
                "subject_ref": note_ref,
                "subject_type": "concept",
                "predicate": "REFERENCES",
                "object_ref": object_ref,
                "object_type": object_type,
                "fact_kind": "explicit_fact",
                "origin_mode": "source_structured",
                "confidence": 0.95,
            }

        fact_hints: list[dict[str, Any]] = [_edge(f"concept:{link}", "concept") for link in wikilinks]
        fact_hints += [_edge(f"topic:{tag}", "topic") for tag in tags]

        return SensorOutputMetadata(
            entities=entities,
            tags=tags,
            fact_hints=fact_hints,
            relation_candidates=[],
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
        skipped_errors = 0
        for path in walk_markdown(vault_root):
            scanned += 1
            rel = path.relative_to(vault_root).as_posix()
            tier = classify_folder(rel, exclude, search_only)
            if tier == "exclude":
                continue
            if tier != self._tier:
                continue  # this instance only handles its own tier
            try:
                mtime = path.stat().st_mtime
                if mtime <= since:
                    continue
                note = parse_note(path, vault_root)
            except Exception:
                # A single unreadable / vanished / locked file must never abort
                # the whole scan — skip it and keep ingesting the rest.
                skipped_errors += 1
                continue
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
            stats={
                "count": len(items),
                "scanned": scanned,
                "skipped_errors": skipped_errors,
                "tier": self._tier,
            },
        )

    async def build_output(self, item: dict) -> SensorOutput:
        body = str(item.get("body") or "")
        # P3: L1 keeps a lean summary; the full frozen note body goes to L2 via
        # pinned_payload. Putting only the summary in narration/content blocks
        # keeps the L1 row + metadata small (the body is no longer duplicated there).
        summary = _lean_summary(body)
        title = str(item.get("title") or "").strip() or None
        tags = [str(t) for t in (item.get("tags") or []) if str(t).strip()]
        wikilinks = [str(w) for w in (item.get("wikilinks") or []) if str(w).strip()]
        mtime = float(item.get("mtime") or time.time())

        content_blocks: list[ContentBlock] = [ContentBlock(kind="text", value=summary)]
        content_blocks += [ContentBlock(kind="wikilink", value=w) for w in wikilinks]
        content_blocks += [ContentBlock(kind="tag", value=t) for t in tags]

        output = self._build_output(
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
            narration=self._build_narration(title=title, body=summary),
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
        # Pin the full frozen note body for L2 extraction (read at extraction time,
        # never re-fetched from the vault). Sparse: None when the note is empty.
        output.pinned_payload = body or None
        return output
