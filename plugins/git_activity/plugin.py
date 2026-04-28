"""Git Activity timeline plugin."""
from __future__ import annotations

from collections import Counter
from typing import Any

from magi_plugin_sdk import (
    ExtensionFieldOption,
    ExtensionFieldSpec,
    Plugin,
    SensorSpec,
)

from .reader import is_git_repo
from .sensor import GitActivitySensor

DEFAULT_SETTINGS = {
    "enabled": False,
    "repos": [],
    "sync_interval_minutes": 30,
    "initial_sync_policy": "lookback_days",
    "initial_sync_lookback_days": 30,
    "sensitive_mode": "redact",
    "sensitive_keywords": [],
}


def _budget_int(budget: object | None, key: str, default: int) -> int:
    if budget is None:
        return int(default)
    if isinstance(budget, dict):
        raw = budget.get(key, default)
    else:
        raw = getattr(budget, key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


def _event_provenance(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata_json")
    if not isinstance(metadata, dict):
        return {}
    timeline = metadata.get("timeline")
    if not isinstance(timeline, dict):
        return {}
    provenance = timeline.get("provenance")
    return provenance if isinstance(provenance, dict) else {}


def _repo_name(repo_path: str) -> str:
    normalized = repo_path.replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1] if normalized else "unknown"


def _event_id(event: dict[str, Any]) -> str | None:
    value = str(event.get("event_id") or "").strip()
    return value or None


def _fields(prefix: str) -> list[ExtensionFieldSpec]:
    """Define all settings fields for the Git Activity plugin."""
    return [
        ExtensionFieldSpec(
            key=f"{prefix}.enabled",
            type="switch",
            label="Enabled",
            description="Whether git activity sync is active.",
            default=False,
            section="general",
            surface="timeline",
            order=10,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.repos",
            type="path",
            label="Repositories",
            description="Select one or more Git repository folders to monitor.",
            default=[],
            section="general",
            surface="timeline",
            order=20,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sync_interval_minutes",
            type="number",
            label="Sync Interval (minutes)",
            description="How often to check for new git activity.",
            default=30,
            min=5,
            max=1440,
            section="general",
            surface="timeline",
            order=30,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.initial_sync_policy",
            type="select",
            label="Initial Sync Policy",
            description="How much history to import on first sync.",
            default="lookback_days",
            options=[
                ExtensionFieldOption(label="Full history", value="full"),
                ExtensionFieldOption(label="Lookback days", value="lookback_days"),
                ExtensionFieldOption(label="From now only", value="from_now"),
            ],
            section="sync",
            surface="timeline",
            order=40,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.initial_sync_lookback_days",
            type="number",
            label="Lookback Days",
            description="Days of history to import on first sync.",
            default=30,
            min=1,
            max=365,
            section="sync",
            surface="timeline",
            order=50,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sensitive_mode",
            type="select",
            label="Sensitive Message Mode",
            description="How to handle commit messages with sensitive content.",
            default="redact",
            options=[
                ExtensionFieldOption(label="Redact sensitive parts", value="redact"),
                ExtensionFieldOption(label="Block entirely", value="block"),
            ],
            section="privacy",
            surface="timeline",
            order=60,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sensitive_keywords",
            type="tags",
            label="Additional Sensitive Keywords",
            description="Extra keywords to detect in commit messages (built-in: password, secret, token, etc.)",
            default=[],
            section="privacy",
            surface="timeline",
            order=70,
        ),
    ]


class GitActivityPlugin(Plugin):
    """Registers the Git Activity timeline source."""

    def build_temporal_summary_features(
        self,
        *,
        source_type: str,
        events: list[dict[str, Any]],
        summary_category: str,
        period_start: float,
        period_end: float,
        budget: object | None = None,
    ) -> dict[str, object] | None:
        """Aggregate repository-local activity features for L3 summaries."""
        _ = summary_category, period_start, period_end
        if source_type != "git_activity" or not events:
            return None

        repo_counter: Counter[str] = Counter()
        operation_counter: Counter[str] = Counter()
        author_counter: Counter[str] = Counter()
        representative_event_ids: list[str] = []

        for event in events:
            provenance = _event_provenance(event)
            repo_path = str(provenance.get("repo_path") or "").strip()
            if repo_path:
                repo_counter[_repo_name(repo_path)] += 1
            operation = str(provenance.get("activity_type") or "other").strip() or "other"
            operation_counter[operation] += 1
            author = str(provenance.get("author") or "").strip()
            if author:
                author_counter[author] += 1
            event_id = _event_id(event)
            if event_id and len(representative_event_ids) < 8:
                representative_event_ids.append(event_id)

        covered_event_count = len(events)
        total_event_count = _budget_int(budget, "total_event_count", covered_event_count)
        omitted_event_count = max(0, total_event_count - covered_event_count)
        top_repos = [
            {"repo": repo, "event_count": count}
            for repo, count in repo_counter.most_common(5)
        ]
        top_operations = [
            {"operation": operation, "event_count": count}
            for operation, count in operation_counter.most_common(5)
        ]

        summary_lines = [
            f"Git feature coverage used {covered_event_count} events across {len(repo_counter)} repositories."
        ]
        if top_repos:
            joined = ", ".join(f"{item['repo']} ({item['event_count']})" for item in top_repos[:3])
            summary_lines.append(f"Most active repositories: {joined}.")
        if top_operations:
            joined = ", ".join(f"{item['operation']} ({item['event_count']})" for item in top_operations[:3])
            summary_lines.append(f"Git operations clustered around: {joined}.")
        if author_counter:
            summary_lines.append(f"Git activity involved {len(author_counter)} authors in the covered events.")
        if omitted_event_count > 0:
            summary_lines.append(
                f"Git feature coverage used {covered_event_count} representative events; {omitted_event_count} additional events were compacted."
            )

        return {
            "feature_type": "git_activity",
            "event_count": covered_event_count,
            "total_event_count": total_event_count,
            "covered_event_count": covered_event_count,
            "omitted_event_count": omitted_event_count,
            "coverage_ratio": (covered_event_count / total_event_count) if total_event_count else None,
            "repo_count": len(repo_counter),
            "operation_count": len(operation_counter),
            "top_entities": [{"type": "repository", **item} for item in top_repos],
            "top_operations": top_operations,
            "author_count": len(author_counter),
            "representative_event_ids": representative_event_ids,
            "summary_lines": summary_lines,
        }

    def get_sensors(self) -> list[tuple[str, object, SensorSpec]]:
        """Get sensor specifications for Git Activity.

        Returns:
            List of sensor tuples (sensor_id, sensor_instance, sensor_spec)
        """
        # Get settings
        settings = {}
        sensors_settings = self.settings.get("sensors", {})
        if isinstance(sensors_settings, dict):
            settings = dict(sensors_settings.get("git_activity", {}))

        source_enabled = bool(settings.get("enabled", DEFAULT_SETTINGS["enabled"]))

        # Get configured repos (use empty list as default)
        repos = settings.get("repos", []) if source_enabled else []
        valid_repos = []
        for repo in repos:
            if isinstance(repo, str) and repo.strip() and is_git_repo(repo):
                valid_repos.append(repo.strip())

        # Create sensor with available repos (may be empty)
        sensor = GitActivitySensor(
            retention_mode="analyze_only",
            repos=valid_repos,
        )

        # Get sync interval
        sync_interval_minutes = settings.get("sync_interval_minutes", DEFAULT_SETTINGS["sync_interval_minutes"])

        return [
            (
                "timeline.git_activity",
                sensor,
                SensorSpec(
                    sensor_id="timeline.git_activity",
                    display_name="Git Activity",
                    description="Git repository activity ingestion for the timeline.",
                    domain="timeline",
                    surface="timeline",
                    sync_mode="interval",
                    polling_mode="interval",
                    fields=_fields("sensors.git_activity"),
                    metadata={
                        "source_type": "git_activity",
                        "default_settings": dict(DEFAULT_SETTINGS),
                        "sync_interval_minutes": sync_interval_minutes,
                    },
                ),
            )
        ]
