"""Timeline sensor for Git Activity."""
from __future__ import annotations

import json
import hashlib
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from magi_plugin_sdk.sensors import (
    ContentBlock,
    SensorBase,
    SensorMemoryPolicy,
    SensorOutput,
    SensorSyncContext,
    SensorSyncResult,
)

from .filters import SensitiveMessageFilter
from .normalizers import normalize_git_activity
from .reader import GitReflogReader, is_git_repo


DEFAULT_SESSION_WINDOW_SECONDS = 30 * 60
DEFAULT_MAX_MESSAGES_PER_SESSION = 5


class GitActivitySensor(SensorBase):
    """Timeline sensor for Git Activity data."""

    sensor_id = "timeline.git_activity"
    display_name = "Git Activity"
    source_type = "git_activity"
    polling_mode = "interval"
    default_interval = 30  # 30 minutes
    update_key_fields = ("source_item_id",)
    relation_edge_whitelist = ("COMMITTED", "CHECKED_OUT", "MERGED", "REBASED")
    supports_pull_sync = True

    memory_policy = SensorMemoryPolicy(
        cognition_eligible=True,
        importance_bias=0.35,
    )

    def __init__(
        self,
        *,
        retention_mode: Optional[str] = None,
        repos: Optional[list[str]] = None,
        l3_summary_enabled: bool = True,
    ):
        super().__init__()
        self.retention_mode = retention_mode or "analyze_only"
        self._repos = repos or []
        self._readers: dict[str, Optional[GitReflogReader]] = {}
        self.memory_policy = SensorMemoryPolicy(
            cognition_eligible=bool(l3_summary_enabled),
            importance_bias=0.35,
        )

    def _get_reader(self, repo_path: str) -> Optional[GitReflogReader]:
        """Get or create a reader for a repository."""
        if repo_path not in self._readers:
            expanded_path = str(Path(repo_path).expanduser().resolve())
            reader = GitReflogReader(expanded_path)
            if reader.is_available():
                self._readers[repo_path] = reader
            else:
                self._readers[repo_path] = None
        return self._readers.get(repo_path)

    def _get_filter(self, settings: dict[str, Any]) -> SensitiveMessageFilter:
        """Get or create the sensitive message filter."""
        mode = settings.get("sensitive_mode", "redact")
        additional_keywords = settings.get("sensitive_keywords", [])
        return SensitiveMessageFilter(
            mode=mode,
            additional_keywords=additional_keywords,
        )

    def source_item_identity(self, item: dict) -> str:
        """Generate unique identity for a source item."""
        source_item_id = str(item.get("source_item_id") or "").strip()
        if source_item_id:
            return source_item_id

        provenance = item.get("provenance") if isinstance(item.get("provenance"), dict) else {}
        repo = str(item.get("repo_path") or provenance.get("repo_path") or "unknown")
        repo_hash = self._repo_hash(repo)
        session_start = self._coerce_timestamp(item.get("session_start_ts") or provenance.get("session_start_ts"))
        session_end = self._coerce_timestamp(item.get("session_end_ts") or provenance.get("session_end_ts"))
        if session_start and session_end:
            return f"git_session_{repo_hash}_{int(session_start)}_{int(session_end)}"

        new_sha = str(item.get("new_sha") or provenance.get("new_sha") or "")
        timestamp = self._coerce_timestamp(item.get("timestamp") or provenance.get("timestamp"))
        return f"git_{repo_hash}_{new_sha[:8]}_{int(timestamp or 0)}"

    def source_item_version_fingerprint(self, item: dict) -> str:
        """Generate version fingerprint for change detection."""
        provenance = item.get("provenance") if isinstance(item.get("provenance"), dict) else {}
        version_parts = [
            self.source_item_identity(item),
            json.dumps(item.get("operation_counts") or provenance.get("operation_counts") or {}, sort_keys=True),
            str(item.get("activity_count") or provenance.get("activity_count") or ""),
            str(item.get("last_sha") or provenance.get("last_sha") or item.get("new_sha") or provenance.get("new_sha") or ""),
            json.dumps(item.get("representative_messages") or provenance.get("representative_messages") or [], sort_keys=True),
        ]
        return hashlib.sha1("|".join(version_parts).encode("utf-8")).hexdigest()

    def _cursor_key(self, repo_path: str) -> str:
        """Return the normalized key used in the per-repository cursor."""
        return str(Path(repo_path).expanduser().resolve())

    def _repo_hash(self, repo_path: str) -> str:
        """Return a short stable hash for a repository path."""
        normalized = str(repo_path or "unknown").replace("\\", "/").rstrip("/")
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]

    def _coerce_timestamp(self, value: Any) -> float | None:
        """Convert common timestamp representations into Unix seconds."""
        if isinstance(value, datetime):
            return float(value.timestamp())
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value.strip():
            try:
                return float(value)
            except ValueError:
                return None
        return None

    def _initial_start_timestamp(
        self,
        context: SensorSyncContext,
        *,
        lookback_days: int,
        initial_policy: str,
    ) -> float | None:
        """Resolve the default start timestamp for repos without a cursor."""
        if context.last_cursor:
            try:
                return float(context.last_cursor)
            except (ValueError, TypeError):
                pass
        if initial_policy == "from_now":
            return datetime.now().timestamp()
        if initial_policy == "full":
            return None
        return (datetime.now() - timedelta(days=lookback_days)).timestamp()

    def _decode_cursor(
        self,
        cursor: str | None,
        *,
        default_start_timestamp: float | None,
    ) -> dict[str, float | None]:
        """Decode the scheduler cursor into per-repository timestamps."""
        if not cursor:
            return {}
        text = str(cursor).strip()
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            legacy_timestamp = self._coerce_timestamp(text)
            return {"*": legacy_timestamp if legacy_timestamp is not None else default_start_timestamp}
        if not isinstance(payload, dict):
            return {}
        repos = payload.get("repos")
        if not isinstance(repos, dict):
            return {}
        decoded: dict[str, float | None] = {}
        for repo_path, timestamp in repos.items():
            decoded[str(repo_path)] = self._coerce_timestamp(timestamp)
        return decoded

    def _encode_cursor(self, repo_timestamps: dict[str, float]) -> str | None:
        """Encode per-repository cursor timestamps for scheduler storage."""
        repos = {
            repo_path: float(timestamp)
            for repo_path, timestamp in sorted(repo_timestamps.items())
            if float(timestamp or 0.0) > 0.0
        }
        if not repos:
            return None
        return json.dumps({"version": 1, "repos": repos}, sort_keys=True)

    def _operation_summary(self, operation_counts: dict[str, int]) -> str:
        """Render compact operation counts for narration and content blocks."""
        parts = []
        for operation, count in Counter(operation_counts).most_common():
            if count <= 0:
                continue
            parts.append(operation if count == 1 else f"{operation} {count}")
        return ", ".join(parts) if parts else "activity"

    def _format_time_range(self, start_ts: float, end_ts: float) -> str:
        """Render a compact local time range."""
        start = datetime.fromtimestamp(start_ts)
        end = datetime.fromtimestamp(end_ts)
        if start.date() == end.date():
            return f"{start:%Y-%m-%d %H:%M}-{end:%H:%M}"
        return f"{start:%Y-%m-%d %H:%M}-{end:%Y-%m-%d %H:%M}"

    def _aggregate_sessions(
        self,
        items: list[dict[str, Any]],
        *,
        session_window_seconds: float,
        max_messages: int,
    ) -> list[dict[str, Any]]:
        """Aggregate low-level reflog items into repository work sessions."""
        if not items:
            return []

        sessions: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for item in sorted(items, key=lambda value: self._coerce_timestamp(value.get("timestamp")) or 0.0):
            item_ts = self._coerce_timestamp(item.get("timestamp")) or 0.0
            if current is None:
                current = self._new_session(item, max_messages=max_messages)
                continue

            current_end = float(current.get("session_end_ts") or 0.0)
            if item_ts - current_end > session_window_seconds:
                sessions.append(self._finalize_session(current))
                current = self._new_session(item, max_messages=max_messages)
                continue

            self._merge_session(current, item, max_messages=max_messages)

        if current is not None:
            sessions.append(self._finalize_session(current))
        return sessions

    def _new_session(self, item: dict[str, Any], *, max_messages: int) -> dict[str, Any]:
        """Create a new session aggregate from one reflog item."""
        timestamp = self._coerce_timestamp(item.get("timestamp")) or time.time()
        repo_path = str(item.get("repo_path") or "")
        repo_name = Path(repo_path).name if repo_path else "unknown"
        activity_type = str(item.get("activity_type") or "other")
        message = str(item.get("message") or "").strip()
        author = str(item.get("author") or "").strip()
        return {
            "repo_path": repo_path,
            "repo_name": repo_name,
            "activity_type": "session",
            "session_start_ts": timestamp,
            "session_end_ts": timestamp,
            "timestamp": datetime.fromtimestamp(timestamp),
            "modified_at": timestamp,
            "operation_counts": {activity_type: 1},
            "operation_summary": self._operation_summary({activity_type: 1}),
            "activity_count": 1,
            "first_sha": str(item.get("old_sha") or ""),
            "last_sha": str(item.get("new_sha") or ""),
            "old_sha": str(item.get("old_sha") or ""),
            "new_sha": str(item.get("new_sha") or ""),
            "authors": [author] if author else [],
            "representative_messages": [message] if message and max_messages > 0 else [],
            "sensitive_redacted": bool(item.get("sensitive_redacted")),
        }

    def _merge_session(self, session: dict[str, Any], item: dict[str, Any], *, max_messages: int) -> None:
        """Merge one reflog item into an existing session aggregate."""
        timestamp = self._coerce_timestamp(item.get("timestamp")) or float(session.get("session_end_ts") or 0.0)
        activity_type = str(item.get("activity_type") or "other")
        operation_counts = dict(session.get("operation_counts") or {})
        operation_counts[activity_type] = int(operation_counts.get(activity_type) or 0) + 1
        session["operation_counts"] = operation_counts
        session["operation_summary"] = self._operation_summary(operation_counts)
        session["activity_count"] = int(session.get("activity_count") or 0) + 1
        session["session_end_ts"] = max(float(session.get("session_end_ts") or 0.0), timestamp)
        session["timestamp"] = datetime.fromtimestamp(float(session["session_end_ts"]))
        session["modified_at"] = float(session["session_end_ts"])
        session["last_sha"] = str(item.get("new_sha") or session.get("last_sha") or "")
        session["new_sha"] = session["last_sha"]
        session["sensitive_redacted"] = bool(session.get("sensitive_redacted")) or bool(item.get("sensitive_redacted"))

        author = str(item.get("author") or "").strip()
        authors = list(session.get("authors") or [])
        if author and author not in authors:
            authors.append(author)
        session["authors"] = authors[:8]

        message = str(item.get("message") or "").strip()
        messages = list(session.get("representative_messages") or [])
        if message and message not in messages and len(messages) < max_messages:
            messages.append(message)
        session["representative_messages"] = messages

    def _finalize_session(self, session: dict[str, Any]) -> dict[str, Any]:
        """Finalize derived fields for a session aggregate."""
        finalized = dict(session)
        start_ts = float(finalized.get("session_start_ts") or finalized.get("session_end_ts") or time.time())
        end_ts = float(finalized.get("session_end_ts") or start_ts)
        finalized["source_item_id"] = self.source_item_identity(finalized)
        finalized["time_range"] = self._format_time_range(start_ts, end_ts)
        finalized["operation_summary"] = self._operation_summary(dict(finalized.get("operation_counts") or {}))
        return finalized

    async def collect_items(self, context: SensorSyncContext) -> SensorSyncResult:
        """Collect git activity data from configured repositories."""
        sensor_settings = (
            context.plugin_settings.get("sensors", {}).get(self.source_type, {})
            if isinstance(context.plugin_settings.get("sensors", {}), dict)
            else {}
        )

        # Get configured repositories
        repos = sensor_settings.get("repos", self._repos)
        if not repos:
            return SensorSyncResult(
                items=[],
                next_cursor=None,
                watermark_ts=time.time(),
                stats={"count": 0, "error": "No repositories configured"},
            )

        # Get settings
        lookback_days = int(sensor_settings.get("initial_sync_lookback_days", 30))

        initial_policy = sensor_settings.get("initial_sync_policy", "lookback_days")

        default_start_timestamp = self._initial_start_timestamp(
            context,
            lookback_days=lookback_days,
            initial_policy=initial_policy,
        )
        cursor_by_repo = self._decode_cursor(
            context.last_cursor,
            default_start_timestamp=default_start_timestamp,
        )
        session_window_seconds = max(
            60.0,
            float(sensor_settings.get("session_window_minutes", DEFAULT_SESSION_WINDOW_SECONDS // 60)) * 60.0,
        )
        max_messages = max(0, int(sensor_settings.get("max_messages_per_session", DEFAULT_MAX_MESSAGES_PER_SESSION)))

        # Get filter
        msg_filter = self._get_filter(sensor_settings)

        # Collect from all repos
        all_items: list[dict[str, Any]] = []
        latest_timestamp = context.last_success_at or 0
        latest_by_repo: dict[str, float] = {}
        errors = []
        processed_repos = 0
        raw_activity_count = 0
        blocked_count = 0

        for repo_path in repos:
            repo_path = str(repo_path).strip()
            if not repo_path:
                continue

            # Validate repo
            if not is_git_repo(repo_path):
                errors.append(f"Invalid repo: {repo_path}")
                continue

            # Get reader
            reader = self._get_reader(repo_path)
            if reader is None:
                errors.append(f"Cannot read repo: {repo_path}")
                continue

            try:
                repo_cursor_key = self._cursor_key(repo_path)
                start_timestamp = cursor_by_repo.get(
                    repo_cursor_key,
                    cursor_by_repo.get("*", default_start_timestamp),
                )
                repo_latest_timestamp = float(start_timestamp or 0.0)
                activities = reader.read_activities(
                    start_timestamp=start_timestamp,
                    limit=context.limit,
                )

                repo_items: list[dict[str, Any]] = []
                for activity in activities:
                    ts = activity.timestamp.timestamp()
                    if ts > repo_latest_timestamp:
                        repo_latest_timestamp = ts

                    # Apply sensitive message filter
                    processed = msg_filter.process(activity.message)
                    if processed is None:
                        blocked_count += 1
                        continue

                    item = {
                        "repo_path": activity.repo_path,
                        "activity_type": activity.activity_type,
                        "old_sha": activity.old_sha,
                        "new_sha": activity.new_sha,
                        "message": processed,
                        "author": activity.author,
                        "timestamp": activity.timestamp,
                        "sensitive_redacted": processed != activity.message,
                    }
                    repo_items.append(item)

                sessions = self._aggregate_sessions(
                    repo_items,
                    session_window_seconds=session_window_seconds,
                    max_messages=max_messages,
                )
                all_items.extend(sessions)
                raw_activity_count += len(repo_items)
                processed_repos += 1
                latest_by_repo[repo_cursor_key] = repo_latest_timestamp
                if repo_latest_timestamp > latest_timestamp:
                    latest_timestamp = repo_latest_timestamp

            except Exception as e:
                errors.append(f"Error reading {repo_path}: {e}")

        # Sort sessions by end time (most recent first)
        all_items.sort(key=lambda x: float(x.get("session_end_ts") or x.get("modified_at") or 0.0), reverse=True)

        # Determine next cursor
        next_cursor = self._encode_cursor(latest_by_repo)

        return SensorSyncResult(
            items=all_items,
            next_cursor=next_cursor,
            watermark_ts=latest_timestamp or time.time(),
            stats={
                "count": len(all_items),
                "raw_activity_count": raw_activity_count,
                "blocked_count": blocked_count,
                "repos_processed": processed_repos,
                "session_window_minutes": int(session_window_seconds // 60),
                "errors": errors if errors else None,
            },
        )

    async def build_output(self, item: dict) -> SensorOutput:
        """Build a SensorOutput from a git activity item."""
        # Normalize the item
        normalized_data = normalize_git_activity(item, self)

        occurred_at = float(normalized_data.get("occurred_at") or time.time())

        # Build content blocks
        repo_path = item.get("repo_path", "")
        repo_name = Path(repo_path).name if repo_path else "unknown"
        activity_type = str(item.get("activity_type", "other"))
        provenance = normalized_data["provenance"]
        operation_summary = str(provenance.get("operation_summary") or item.get("operation_summary") or activity_type)
        time_range = str(provenance.get("time_range") or item.get("time_range") or "")
        first_sha = str(provenance.get("first_sha") or item.get("first_sha") or item.get("old_sha") or "")
        last_sha = str(provenance.get("last_sha") or item.get("last_sha") or item.get("new_sha") or "")
        representative_messages = [str(message) for message in provenance.get("representative_messages", []) if str(message).strip()]

        # Get translated activity type name
        activity_type_name = self.t(
            f"activity_types.{activity_type}",
            fallback=activity_type,
        )

        content_blocks = [
            ContentBlock(kind="text", value=self.t("content_blocks.repo", repo_path=repo_path)),
            ContentBlock(
                kind="text",
                value=self.t("content_blocks.operations", operations=operation_summary, fallback=f"Operations: {operation_summary}"),
            ),
        ]
        if time_range:
            content_blocks.append(
                ContentBlock(
                    kind="text",
                    value=self.t("content_blocks.time_range", time_range=time_range, fallback=f"Time: {time_range}"),
                )
            )
        if first_sha or last_sha:
            content_blocks.append(
                ContentBlock(
                    kind="text",
                    value=self.t(
                        "content_blocks.commit_range",
                        first_sha=first_sha[:8],
                        last_sha=last_sha[:8],
                        fallback=f"Commits: {first_sha[:8]}..{last_sha[:8]}",
                    ),
                )
            )
        if representative_messages:
            content_blocks.append(
                ContentBlock(
                    kind="text",
                    value=self.t(
                        "content_blocks.messages",
                        messages="; ".join(representative_messages[:3]),
                        fallback=f"Messages: {'; '.join(representative_messages[:3])}",
                    ),
                )
            )

        return self._build_output(
            source_item_id=normalized_data["source_item_id"],
            activity=self._build_activity(
                source=self._build_activity_facet(
                    code="git",
                    i18n_key="activity.source.git",
                    fallback="Git",
                    embedding_fallback="Git",
                ),
                action=self._build_activity_facet(
                    code=activity_type,
                    i18n_key=f"activity_types.{activity_type}",
                    fallback=activity_type_name,
                    embedding_fallback=activity_type_name,
                ),
            ),
            narration=self._build_narration(
                title=normalized_data["title"],
                body=normalized_data["summary"],
            ),
            occurred_at=occurred_at,
            content_blocks=content_blocks,
            tags=normalized_data["tags"],
            provenance={
                "sensor_id": self.sensor_id,
                **provenance,
            },
            domain_payload={"retention_mode": self.retention_mode},
        )
