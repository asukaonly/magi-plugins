"""Git Activity timeline plugin."""
from __future__ import annotations

from collections import Counter
from typing import Any

from magi_plugin_sdk import (
    ExtractionProfileSpec,
    Plugin,
    SourceSpec,
)

from .reader import is_git_repo
from .source import GitActivitySource

DEFAULT_SETTINGS = {
    "enabled": False,
    "repos": [],
    "sync_interval_minutes": 30,
    "initial_sync_policy": "lookback_days",
    "initial_sync_lookback_days": 30,
    "initial_sync_configured": False,
    "session_window_minutes": 30,
    "max_messages_per_session": 5,
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
    activity_snapshot = metadata.get("activity_snapshot")
    if not isinstance(activity_snapshot, dict):
        return {}
    provenance = activity_snapshot.get("provenance")
    return provenance if isinstance(provenance, dict) else {}


def _repo_name(repo_path: str) -> str:
    normalized = repo_path.replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1] if normalized else "unknown"


def _event_id(event: dict[str, Any]) -> str | None:
    value = str(event.get("event_id") or "").strip()
    return value or None


def _int_from_mapping(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _operation_counts(provenance: dict[str, Any]) -> dict[str, int]:
    raw_counts = provenance.get("operation_counts")
    if not isinstance(raw_counts, dict):
        operation = str(provenance.get("activity_type") or "other").strip() or "other"
        return {operation: 1}
    counts: dict[str, int] = {}
    for operation, count in raw_counts.items():
        normalized = str(operation or "other").strip() or "other"
        amount = _int_from_mapping(count)
        if amount > 0:
            counts[normalized] = counts.get(normalized, 0) + amount
    return counts


class GitActivityPlugin(Plugin):
    """Registers the Git Activity timeline source."""

    def get_extraction_profiles(self) -> list[ExtractionProfileSpec]:
        return [
            ExtractionProfileSpec(
                profile_id="source.git_activity",
                source_types=["git_activity"],
                allowed_entity_types=["software", "technology", "topic"],
                allowed_predicates=["COMMITTED", "CHECKED_OUT", "MERGED", "REBASED", "WORKS_WITH", "USES"],
                structured_allowed_entity_types=["software", "technology", "topic"],
                structured_allowed_predicates=["COMMITTED", "CHECKED_OUT", "MERGED", "REBASED", "WORKS_WITH", "USES"],
                allowed_assertion_families=["project_profile"],
                allow_graph=True,
                allow_assertion=True,
                allowed_assertion_traits=["project.*"],
                derived_assertion_specs=[
                    {
                        "rule_id": "git_activity.recurring_project",
                        "source_predicates": ["COMMITTED"],
                        "source_types": ["git_activity"],
                        "trait_family": "project_profile",
                        "trait_name_template": "project.{object_slug}",
                        "min_observations": 2,
                        "min_distinct_days": 2,
                        "signal_preset": "sustained_engagement",
                        "durable_permitted": True,
                        "durable_min_observations": 6,
                        "durable_min_distinct_days": 3,
                        "durable_min_span_days": 14,
                        "object_types": ["software"],
                        "source_domains": ["external_activity"],
                        "value_strategy": "canonical_name",
                    }
                ],
                extraction_instructions=(
                    "These events are git operations (commits, checkouts, merges, rebases).\n"
                    "Focus on extracting the repository/project as a `software` entity and\n"
                    "any technologies or frameworks mentioned in commit messages.\n\n"
                    "Entity extraction rules:\n"
                    "- Extract the repository name as a `software` entity.\n"
                    "- Extract programming languages and frameworks as `technology` entities\n"
                    "  only when clearly visible in commit context.\n"
                    "- COMMITTED: for commit operations.\n"
                    "- WORKS_WITH: for technologies used in the project.\n"
                    "- USES: for tools and platforms (GitHub, GitLab).\n"
                    "- Do NOT extract individual file paths, function names, or branch names\n"
                    "  as entities.\n\n"
                    "Assertion rules:\n"
                    "- Do not emit Phase 2 assertion candidates for git events. Repeated\n"
                    "  COMMITTED graph evidence may be aggregated later by the host-owned\n"
                    "  derived project rule declared in this profile."
                ),
            )
        ]

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
        total_activity_count = 0

        for event in events:
            provenance = _event_provenance(event)
            repo_path = str(provenance.get("repo_path") or "").strip()
            activity_count = max(1, _int_from_mapping(provenance.get("activity_count"), 1))
            total_activity_count += activity_count
            for operation, count in _operation_counts(provenance).items():
                operation_counter[operation] += count
            if repo_path:
                repo_counter[_repo_name(repo_path)] += activity_count
            authors = provenance.get("authors")
            if isinstance(authors, list):
                for author in authors:
                    author_text = str(author or "").strip()
                    if author_text:
                        author_counter[author_text] += 1
            else:
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
            {"repo": repo, "operation_count": count, "event_count": count}
            for repo, count in repo_counter.most_common(5)
        ]
        top_operations = [
            {"operation": operation, "operation_count": count, "event_count": count}
            for operation, count in operation_counter.most_common(5)
        ]

        summary_lines = [
            f"Git feature coverage used {covered_event_count} timeline sessions covering {total_activity_count} Git operations across {len(repo_counter)} repositories."
        ]
        if top_repos:
            joined = ", ".join(f"{item['repo']} ({item['operation_count']})" for item in top_repos[:3])
            summary_lines.append(f"Most active repositories by Git operation count: {joined}.")
        if top_operations:
            joined = ", ".join(f"{item['operation']} ({item['operation_count']})" for item in top_operations[:3])
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
            "session_count": covered_event_count,
            "activity_count": total_activity_count,
            "git_operation_count": total_activity_count,
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

    def get_sources(self) -> list[tuple[str, object, SourceSpec]]:
        """Get source specifications for Git Activity.

        Returns:
            List of source tuples (source_id, source_instance, source_spec)
        """
        # Get settings
        settings = {}
        sources_settings = self.settings.get("sources", {})
        if isinstance(sources_settings, dict):
            settings = dict(sources_settings.get("git_activity", {}))

        source_enabled = bool(settings.get("enabled", DEFAULT_SETTINGS["enabled"]))

        # Get configured repos (use empty list as default)
        repos = settings.get("repos", []) if source_enabled else []
        valid_repos = []
        for repo in repos:
            if isinstance(repo, str) and repo.strip() and is_git_repo(repo):
                valid_repos.append(repo.strip())

        # Create source with available repos (may be empty)
        source = GitActivitySource(
            retention_mode="analyze_only",
            repos=valid_repos,
            l3_summary_enabled=True,
        )

        # Get sync interval
        sync_interval_minutes = settings.get("sync_interval_minutes", DEFAULT_SETTINGS["sync_interval_minutes"])

        return [
            (
                "timeline.git_activity",
                source,
                SourceSpec(
                    source_id="timeline.git_activity",
                    display_name="Git Activity",
                    description="Git repository activity ingestion for the timeline.",
                    domain="timeline",
                    surface="timeline",
                    sync_mode="interval",
                    polling_mode="interval",
                    fields=[
                        field.model_copy(deep=True)
                        for field in sorted(self.manifest.settings_fields, key=lambda field: field.order)
                        if field.surface == "timeline" and field.section != "activation"
                    ],
                    metadata={
                        "source_type": "git_activity",
                        "default_settings": {
                            field.key.rsplit(".", 1)[-1]: field.model_copy(deep=True).default
                            for field in self.manifest.settings_fields
                            if field.key.startswith("sources.") and field.type != "secret"
                        },
                        "activation_flow": self.manifest.activation_flow.model_dump() if self.manifest.activation_flow is not None else None,
                        "sync_interval_minutes": sync_interval_minutes,
                    },
                ),
            )
        ]
