# sensor.py
"""Agent history sensor: ingest the user's OWN turns from local agent transcripts,
scrub secrets, and emit them as first-person events so L2 mines them into the
user's professional profile.

Authorship crux (mirrors obsidian-vault): ``memory_policy.author_type="user"`` +
``memory_domain="user_authored"`` make L2 render the content with the ``[USER]``
tag, which Rule 1 extracts as the user's own facts. A default sensor
(``author_type="external"`` -> ``[EXTERNAL]``) would NOT be extracted -- so this
policy is the whole point of the sensor.

Sync model: the plugin registers one pull-sync source per agent. Each sensor
uses the same adapter-pluggable pipeline but filters to its configured agent, so
Claude Code and Codex can be enabled/configured independently while still sharing
one implementation. ``collect_items`` dispatches each configured ``source_path``
through ``select_adapter``, applies the first-sync lookback window
(``initial_sync_lookback_days``) on the first run only, and advances an
mtime/occurred_at cursor so later runs are forward-incremental. ``build_output``
scrubs every user turn (``redact_secrets``) and pins the full joined text in
``pinned_payload`` for L2; L1's narration stays a lean summary. Dedup is by
``source_item_identity`` (``agent:session_id``) +
``source_item_version_fingerprint`` (changes when the session grows).
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from magi_plugin_sdk.sensors import (
    ContentBlock,
    SensorBase,
    SensorMemoryPolicy,
    SensorOutput,
    SensorSyncContext,
    SensorSyncResult,
)

from .adapters.base import select_adapter
from .scrub import redact_secrets

# Cap a single conversation's joined content. Per-session chunking is a future
# enhancement (YAGNI for v1); this just bounds a runaway session's pinned text.
_MAX_CHARS = 24000
_SUMMARY_MAX_CHARS = 280
_DEFAULT_LOOKBACK_DAYS = 30
_SECONDS_PER_DAY = 86400


def _lean_summary(text: str, *, max_chars: int = _SUMMARY_MAX_CHARS) -> str:
    """First non-empty line of the (scrubbed) text, capped -- a lean L1 preview.

    The full joined turns are pinned separately for L2; L1's narration only needs
    a short, timeline-friendly summary.
    """
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped if len(stripped) <= max_chars else stripped[:max_chars].rstrip() + "…"
    return ""


class CodingAgentHistorySensor(SensorBase):
    """Pull-sync sensor that ingests the user's own agent transcript turns."""

    sensor_id = "timeline.coding_agent_history"
    display_name = "Agent History"
    source_type = "coding_agent_history"
    polling_mode = "interval"
    default_interval = 30  # minutes
    update_key_fields = ("source_item_id",)
    # Unused by the host (it reads L2 fact_hints, not the timeline relation
    # whitelist) -- kept empty to avoid implying a route we don't use.
    relation_edge_whitelist = ()
    supports_pull_sync = True

    def __init__(self, *, agent: str, source_type: str, display_name: str) -> None:
        super().__init__()
        self.agent = str(agent)
        self.source_type = str(source_type)
        self.sensor_id = f"timeline.{self.source_type}"
        self.display_name = str(display_name)
        # THE CRUX: author_type="user" + memory_domain="user_authored" => L2 renders
        # [USER] and mines these as the user's own facts (mirrors obsidian-vault).
        # Transcripts are higher-volume / lower-signal than hand-authored notes, so
        # they are compressible (vs obsidian's permanent) but still cognition-eligible.
        self.memory_policy = SensorMemoryPolicy(
            memory_domain="user_authored",
            ingest_target="l1_only",
            cognition_eligible=True,
            retention_class="compressible",
            importance_bias=0.5,
            author_type="user",
            content_type="text",
        )

    def source_item_identity(self, item: dict[str, Any]) -> str:
        """Stable id for supersession: ``agent:session_id`` (set at collect time)."""
        return str(item.get("source_item_id") or "")

    def source_item_version_fingerprint(self, item: dict[str, Any]) -> str:
        """Fingerprint that changes when a session grows (new turns => re-ingest)."""
        turns = item.get("user_turns") or []
        parts = [
            self.source_item_identity(item),
            str(len(turns)),
            str(item.get("occurred_at") or ""),
        ]
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()

    def _resolve_settings(self, context: SensorSyncContext) -> dict[str, Any]:
        """Pull this sensor's settings out of the plugin settings tree."""
        settings = context.plugin_settings
        sensors = settings.get("sensors", {}) if isinstance(settings, dict) else {}
        cfg = sensors.get(self.source_type, {}) if isinstance(sensors, dict) else {}
        if not cfg and isinstance(sensors, dict):
            # Backward compatibility for users who enabled the earlier single
            # coding_agent_history source before entries were split by agent.
            cfg = sensors.get("coding_agent_history", {})
        return cfg if isinstance(cfg, dict) else {}

    async def collect_items(self, context: SensorSyncContext) -> SensorSyncResult:
        cfg = self._resolve_settings(context)
        source_paths = [str(p) for p in (cfg.get("source_paths") or []) if str(p).strip()]
        if not source_paths:
            return SensorSyncResult(
                items=[],
                next_cursor=context.last_cursor,
                watermark_ts=time.time(),
                stats={"count": 0, "reason": "no_source_paths"},
            )

        try:
            since_mtime = float(context.last_cursor) if context.last_cursor else 0.0
        except (TypeError, ValueError):
            since_mtime = 0.0

        # The lookback window gates the FIRST sync only (no cursor yet); later syncs
        # are purely forward-incremental via the mtime cursor.
        is_first_sync = not context.last_cursor
        cutoff_ts = 0.0
        if is_first_sync:
            try:
                days = int(cfg.get("initial_sync_lookback_days", _DEFAULT_LOOKBACK_DAYS))
            except (TypeError, ValueError):
                days = _DEFAULT_LOOKBACK_DAYS
            if days > 0:
                cutoff_ts = time.time() - days * _SECONDS_PER_DAY

        limit = int(context.limit or 1000)
        items: list[dict[str, Any]] = []
        max_mtime = since_mtime
        scanned_paths = 0
        for path in source_paths:
            adapter = select_adapter(path)
            if adapter is None:
                continue  # path matches no known agent layout
            if getattr(adapter, "agent", None) != self.agent:
                continue
            scanned_paths += 1
            for conv in adapter.iter_conversations(
                path, since_mtime=since_mtime, cutoff_ts=cutoff_ts
            ):
                items.append(
                    {
                        "source_item_id": f"{conv.agent}:{conv.session_id}",
                        "agent": conv.agent,
                        "occurred_at": conv.occurred_at,
                        "user_turns": list(conv.user_turns),
                        "project_hint": conv.project_hint,
                        "native_path": conv.native_path,
                    }
                )
                # Advance the cursor by each conversation's recency proxy. occurred_at
                # is the session's newest user-turn ts, which mirrors the file mtime
                # the adapters gate on, so a future sync's since_mtime is correct.
                max_mtime = max(max_mtime, float(conv.occurred_at or 0.0))
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break

        next_cursor = (
            str(max_mtime)
            if max_mtime > since_mtime
            else (context.last_cursor or str(time.time()))
        )
        return SensorSyncResult(
            items=items,
            next_cursor=next_cursor,
            watermark_ts=max_mtime or time.time(),
            stats={"count": len(items), "scanned_paths": scanned_paths},
        )

    async def build_output(self, item: dict[str, Any]) -> SensorOutput:
        agent = str(item.get("agent") or "coding_assistant")
        # Scrub every turn BEFORE it leaves the sensor: the memory pipeline does no
        # redaction and uploads content to the configured LLM.
        turns = [redact_secrets(str(t)) for t in (item.get("user_turns") or [])]
        full = "\n\n".join(turns)[:_MAX_CHARS]
        summary = _lean_summary(full)
        project = item.get("project_hint")
        occurred_at = float(item.get("occurred_at") or time.time())
        agent_label = agent.replace("_", " ").title()
        title = f"{agent_label} session" + (f" — {project}" if project else "")

        output = self._build_output(
            source_item_id=self.source_item_identity(item),
            activity=self._build_activity(
                source=self._build_activity_facet(
                    code=agent,
                    i18n_key=f"activity.source.{agent}",
                    fallback=agent_label,
                    embedding_fallback=f"{agent_label} coding session",
                ),
                action=self._build_activity_facet(
                    code="conversed",
                    i18n_key="activity.action.conversed",
                    fallback="conversed",
                ),
                object=self._build_activity_facet(
                    code="coding_session",
                    i18n_key="activity.object.coding_session",
                    fallback="coding session",
                ),
                qualifiers={"turn_count": len(turns), "project": project},
            ),
            narration=self._build_narration(title=title, body=summary),
            occurred_at=occurred_at,
            content_blocks=[ContentBlock(kind="text", value=summary)] if summary else [],
            tags=[t for t in ["coding_assistant", agent, project] if t],
            provenance={
                "sensor_id": self.sensor_id,
                "agent": agent,
                "turn_count": len(turns),
                "native_path": item.get("native_path"),
            },
        )
        # Pin the full scrubbed user-turn text for L2 (read at extraction time,
        # never re-fetched). Sparse: None when the session has no usable text.
        output.pinned_payload = full or None
        return output
