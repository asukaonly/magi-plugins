"""Timeline sensor for local GitHub activity pull sync."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from magi_plugin_sdk.sensors import (
    ContentBlock,
    L2BatchPolicy,
    SensorBase,
    SensorMemoryPolicy,
    SensorOutput,
    SensorOutputMetadata,
    SensorSyncContext,
    SensorSyncResult,
)

from .client import GitHubActivityClient, iso_to_timestamp, normalize_repository_slug, timestamp_to_iso


ClientFactory = Callable[[str], GitHubActivityClient]


KIND_LABELS = {
    "pull_request": "Pull request",
    "pull_request_review": "Pull request review",
    "issue": "Issue",
    "commit": "Commit",
    "check_run": "Check run",
}


class GitHubActivitySensor(SensorBase):
    """Pulls selected GitHub repository activity into the timeline."""

    sensor_id = "timeline.github_activity"
    display_name = "GitHub Activity"
    source_type = "github_activity"
    polling_mode = "interval"
    default_interval = 30
    update_key_fields = ("source_item_id",)
    supports_pull_sync = True
    relation_edge_whitelist = ("WORKED_ON", "REVIEWED", "OPENED", "COMMITTED", "CHECKED")
    memory_policy = SensorMemoryPolicy(
        cognition_eligible=True,
        importance_bias=0.45,
        allow_llm_extraction=False,
    )

    def __init__(
        self,
        *,
        access_token: str,
        repositories: list[str],
        initial_sync_lookback_days: int = 30,
        client_factory: ClientFactory | None = None,
    ) -> None:
        super().__init__()
        self.access_token = str(access_token or "").strip()
        self.repositories = _normalize_repositories(repositories)
        self.initial_sync_lookback_days = max(1, int(initial_sync_lookback_days or 30))
        self._client_factory = client_factory or (lambda token: GitHubActivityClient(access_token=token))

    async def collect_items(self, context: SensorSyncContext) -> SensorSyncResult:
        if not self.access_token:
            return SensorSyncResult(items=[], next_cursor=context.last_cursor, stats={"error": "missing_access_token"})
        if not self.repositories:
            return SensorSyncResult(items=[], next_cursor=context.last_cursor, stats={"error": "missing_repositories"})

        since_iso = self._since_iso(context)
        per_repo_limit = max(1, int(context.limit or 50) // max(1, len(self.repositories)))
        client = self._client_factory(self.access_token)
        items: list[dict[str, Any]] = []
        errors: list[str] = []
        for repository in self.repositories:
            try:
                items.extend(client.collect_repository_events(repository, since_iso=since_iso, limit=per_repo_limit))
            except Exception as exc:  # pragma: no cover - defensive runtime reporting
                errors.append(f"{repository}: {exc}")

        items.sort(key=lambda item: iso_to_timestamp(str(item.get("occurred_at") or "")), reverse=True)
        items = items[: max(1, int(context.limit or 50))]
        max_seen = max([iso_to_timestamp(str(item.get("occurred_at") or "")) for item in items] or [time.time()])
        next_cursor = json.dumps({"version": 1, "since": timestamp_to_iso(max_seen)}, sort_keys=True)
        return SensorSyncResult(
            items=items,
            next_cursor=next_cursor,
            watermark_ts=max_seen,
            stats={
                "count": len(items),
                "repositories_processed": len(self.repositories),
                "since": since_iso,
                "errors": errors if errors else None,
            },
        )

    async def build_output(self, item: dict[str, Any]) -> SensorOutput:
        repository = str(item.get("repository") or "").strip()
        event_kind = str(item.get("event_kind") or "activity").strip()
        kind_label = KIND_LABELS.get(event_kind, event_kind.replace("_", " ").title())
        title = str(item.get("title") or kind_label).strip()
        summary = str(item.get("summary") or title).strip()
        state = str(item.get("state") or "").strip()
        actor = str(item.get("actor") or "").strip()
        occurred_at = iso_to_timestamp(str(item.get("occurred_at") or ""))
        content_blocks = [
            ContentBlock(kind="text", value=f"Repository: {repository}"),
            ContentBlock(kind="text", value=f"Activity: {kind_label}"),
        ]
        if state:
            content_blocks.append(ContentBlock(kind="text", value=f"State: {state}"))
        if actor:
            content_blocks.append(ContentBlock(kind="text", value=f"Actor: {actor}"))
        if item.get("url"):
            content_blocks.append(ContentBlock(kind="text", value=f"URL: {item['url']}"))

        return self._build_output(
            source_item_id=str(item.get("source_item_id") or ""),
            activity=self._build_activity(
                source=self._build_activity_facet(
                    code="github",
                    i18n_key="activity.source.github",
                    fallback="GitHub",
                    embedding_fallback="GitHub",
                ),
                action=self._build_activity_facet(
                    code=event_kind,
                    i18n_key=f"activity.action.{event_kind}",
                    fallback=kind_label,
                    embedding_fallback=kind_label,
                ),
                qualifiers={
                    "repository": repository,
                    "state": state,
                    "actor": actor,
                    "number": item.get("number") or "",
                },
            ),
            narration=self._build_narration(
                title=f"{repository}: {title}" if repository else title,
                body=summary,
            ),
            occurred_at=occurred_at,
            content_blocks=content_blocks,
            tags=[tag for tag in ["github", event_kind, repository, state] if tag],
            provenance={
                "sensor_id": self.sensor_id,
                "repository": repository,
                "event_kind": event_kind,
                "state": state,
                "actor": actor,
                "url": str(item.get("url") or ""),
                "sha": str(item.get("sha") or ""),
                "number": item.get("number"),
            },
            domain_payload={"provider": "github", "repository": repository},
        )

    async def extract_metadata(self, item: dict[str, Any]) -> SensorOutputMetadata:
        repository = str(item.get("repository") or "").strip()
        if not repository:
            return SensorOutputMetadata()
        event_kind = str(item.get("event_kind") or "").strip()
        predicate = _predicate_for_kind(event_kind)
        return SensorOutputMetadata(
            entities=[
                {
                    "mention_text": repository,
                    "entity_type": "software",
                    "canonical_name_hint": repository,
                }
            ],
            tags=[tag for tag in ["github", event_kind, repository] if tag],
            fact_hints=[
                {
                    "subject_ref": "user:self",
                    "subject_type": "user",
                    "predicate": predicate,
                    "object_ref": f"software:{repository}",
                    "object_type": "software",
                    "fact_kind": "interaction_evidence",
                    "origin_mode": "source_structured",
                    "confidence": 0.9,
                    "attributes": {
                        "event_kind": event_kind,
                        "state": str(item.get("state") or ""),
                        "actor": str(item.get("actor") or ""),
                    },
                }
            ],
        )

    def l2_batch_policy(self, output: SensorOutput) -> L2BatchPolicy | None:
        repository = str(output.domain_payload.get("repository") or "").strip()
        return L2BatchPolicy(
            owner=f"github_activity:{repository}" if repository else "github_activity",
            max_events=20,
            min_ready_events=3,
            max_wait_seconds=900,
        )

    def _since_iso(self, context: SensorSyncContext) -> str:
        cursor_since = _cursor_since(context.last_cursor)
        if cursor_since:
            return cursor_since
        if context.last_success_at:
            return timestamp_to_iso(context.last_success_at)
        start = datetime.now(timezone.utc) - timedelta(days=self.initial_sync_lookback_days)
        return start.isoformat().replace("+00:00", "Z")


def _normalize_repositories(values: list[str]) -> list[str]:
    repos: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        slug = normalize_repository_slug(str(value))
        if slug and slug not in seen:
            repos.append(slug)
            seen.add(slug)
    return repos


def _cursor_since(cursor: str | None) -> str | None:
    if not cursor:
        return None
    try:
        data = json.loads(cursor)
    except json.JSONDecodeError:
        return str(cursor).strip() or None
    if not isinstance(data, dict):
        return None
    since = str(data.get("since") or "").strip()
    return since or None


def _predicate_for_kind(event_kind: str) -> str:
    if event_kind == "pull_request_review":
        return "REVIEWED"
    if event_kind == "commit":
        return "COMMITTED"
    if event_kind == "check_run":
        return "CHECKED"
    if event_kind in {"pull_request", "issue"}:
        return "WORKED_ON"
    return "WORKED_ON"
