"""Portable Git session observations, versioned independently of the package."""

from typing import Any


def collection_capability() -> dict[str, Any]:
    properties: dict[str, Any] = {
        key: {"type": "string"} for key in (
            "repo_path", "repo_name", "operation_summary", "first_sha", "last_sha",
            "old_sha", "new_sha", "source_item_id", "time_range",
        )
    }
    properties.update({key: {"type": "number"} for key in (
        "session_start_ts", "session_end_ts", "timestamp", "modified_at",
    )})
    properties.update({
        "activity_type": {"const": "session"},
        "operation_counts": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}},
        "activity_count": {"type": "integer", "minimum": 0},
        "authors": {"type": "array", "items": {"type": "string"}},
        "representative_messages": {"type": "array", "items": {"type": "string"}},
        "sensitive_redacted": {"type": "boolean"},
    })
    return {
        "capability_id": "git_activity.observe",
        "protocol": "source.change.v1",
        "schema_id": "git_activity.session",
        "schema_version": 1,
        "direction": "observe",
        "supports_resources": False,
        "payload_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object", "additionalProperties": False,
            "properties": properties, "required": sorted(properties),
        },
    }
