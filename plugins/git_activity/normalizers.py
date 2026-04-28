"""Normalization helpers for Git Activity timeline ingestion."""
from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .types import GitActivity


def normalize_git_activity(item: dict[str, Any] | GitActivity, sensor: Any) -> dict[str, Any]:
    """Normalize git activity data into timeline event format.

    Args:
        item: Git activity data (dict or GitActivity object)
        sensor: The sensor instance

    Returns:
        Dictionary with normalized event data
    """
    if isinstance(item, GitActivity):
        item = {
            "repo_path": item.repo_path,
            "activity_type": item.activity_type,
            "old_sha": item.old_sha,
            "new_sha": item.new_sha,
            "message": item.message,
            "author": item.author,
            "timestamp": item.timestamp,
        }

    repo_path = str(item.get("repo_path") or "")
    repo_name = str(item.get("repo_name") or _repo_name(repo_path))
    activity_type = str(item.get("activity_type") or "other")

    if activity_type == "session" or item.get("operation_counts"):
        return _normalize_git_session(item, sensor, repo_path=repo_path, repo_name=repo_name)

    timestamp = _coerce_datetime(item.get("timestamp"))
    message = str(item.get("message") or "")
    title = message[:100] + "..." if len(message) > 100 else message
    summary = f"[{activity_type}] {message[:50]} @ {repo_name}"
    event_id = str(item.get("source_item_id") or _single_event_id(repo_path, str(item.get("new_sha") or ""), timestamp))
    tags = ["git", activity_type, repo_name]
    provenance = {
        "sensor_id": sensor.sensor_id,
        "repo_path": repo_path,
        "repo_name": repo_name,
        "activity_type": activity_type,
        "old_sha": str(item.get("old_sha") or ""),
        "new_sha": str(item.get("new_sha") or ""),
        "author": str(item.get("author") or ""),
    }

    return {
        "event_id": event_id,
        "source_type": "git_activity",
        "source_item_id": event_id,
        "occurred_at": timestamp.timestamp(),
        "title": title,
        "summary": summary,
        "tags": tags,
        "provenance": provenance,
    }


def _normalize_git_session(
    item: dict[str, Any],
    sensor: Any,
    *,
    repo_path: str,
    repo_name: str,
) -> dict[str, Any]:
    start_ts = _coerce_timestamp(item.get("session_start_ts"))
    end_ts = _coerce_timestamp(item.get("session_end_ts")) or start_ts
    operation_counts = _normalize_counts(item.get("operation_counts"))
    activity_count = int(item.get("activity_count") or sum(operation_counts.values()) or 0)
    operation_summary = str(item.get("operation_summary") or _operation_summary(operation_counts))
    time_range = str(item.get("time_range") or _format_time_range(start_ts, end_ts))
    representative_messages = _normalize_string_list(item.get("representative_messages"), limit=5)
    authors = _normalize_string_list(item.get("authors"), limit=8)
    first_sha = str(item.get("first_sha") or item.get("old_sha") or "")
    last_sha = str(item.get("last_sha") or item.get("new_sha") or "")
    event_id = str(item.get("source_item_id") or _session_event_id(repo_path, start_ts, end_ts))

    title = f"Git activity · {repo_name}"
    summary = f"Worked in {repo_name}: {operation_summary}."
    if activity_count > 1:
        summary = f"Worked in {repo_name}: {operation_summary} across {activity_count} Git operations."

    tags = ["git", "git_session", repo_name]
    tags.extend(operation for operation in operation_counts if operation not in tags)
    provenance = {
        "sensor_id": sensor.sensor_id,
        "repo_path": repo_path,
        "repo_name": repo_name,
        "activity_type": "session",
        "operation_counts": operation_counts,
        "operation_summary": operation_summary,
        "activity_count": activity_count,
        "session_start_ts": start_ts,
        "session_end_ts": end_ts,
        "time_range": time_range,
        "first_sha": first_sha,
        "last_sha": last_sha,
        "authors": authors,
        "representative_messages": representative_messages,
        "sensitive_redacted": bool(item.get("sensitive_redacted")),
    }

    return {
        "event_id": event_id,
        "source_type": "git_activity",
        "source_item_id": event_id,
        "occurred_at": end_ts,
        "title": title,
        "summary": summary,
        "tags": tags,
        "provenance": provenance,
    }


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    timestamp = _coerce_timestamp(value)
    return datetime.fromtimestamp(timestamp or datetime.now().timestamp())


def _coerce_timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        return float(value.timestamp())
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return datetime.now().timestamp()
    return datetime.now().timestamp()


def _repo_name(repo_path: str) -> str:
    return Path(repo_path).name if repo_path else "unknown"


def _repo_hash(repo_path: str) -> str:
    normalized = str(repo_path or "unknown").replace("\\", "/").rstrip("/")
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]


def _single_event_id(repo_path: str, new_sha: str, timestamp: datetime) -> str:
    return f"git_{_repo_hash(repo_path)}_{new_sha[:8]}_{int(timestamp.timestamp())}"


def _session_event_id(repo_path: str, start_ts: float, end_ts: float) -> str:
    return f"git_session_{_repo_hash(repo_path)}_{int(start_ts)}_{int(end_ts)}"


def _normalize_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    counts: Counter[str] = Counter()
    for key, raw_count in value.items():
        operation = str(key or "other").strip() or "other"
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            counts[operation] += count
    return dict(counts)


def _operation_summary(operation_counts: dict[str, int]) -> str:
    parts = []
    for operation, count in Counter(operation_counts).most_common():
        if count <= 0:
            continue
        parts.append(operation if count == 1 else f"{operation} {count}")
    return ", ".join(parts) if parts else "activity"


def _format_time_range(start_ts: float, end_ts: float) -> str:
    start = datetime.fromtimestamp(start_ts)
    end = datetime.fromtimestamp(end_ts)
    if start.date() == end.date():
        return f"{start:%Y-%m-%d %H:%M}-{end:%H:%M}"
    return f"{start:%Y-%m-%d %H:%M}-{end:%Y-%m-%d %H:%M}"


def _normalize_string_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = " ".join(str(item or "").strip().split())
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
        if len(normalized) >= limit:
            break
    return normalized
