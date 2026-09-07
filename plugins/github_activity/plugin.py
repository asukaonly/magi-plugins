"""GitHub Activity timeline plugin."""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

from magi_plugin_sdk import ExtractionProfileSpec, Plugin, PluginSettingsActionResult, PluginSettingsActionSpec, SourceSpec

from .client import (
    GitHubClientError,
    GitHubDeviceAuthClient,
    GitHubDeviceAuthorizationPending,
)
from .source import GitHubActivitySource


CONNECT_ACTION_ID = "connect_github"
GITHUB_CLIENT_ID_ENV = "MAGI_GITHUB_ACTIVITY_CLIENT_ID"
DEFAULT_GITHUB_CLIENT_ID = "Ov23liOlYZ2ibhh1I65w"
L2_PREDICATES = ["WORKS_WITH", "COMMITTED", "USES", "REFERENCES"]
DEFAULT_SETTINGS = {
    "enabled": False,
    "client_id": "",
    "repositories": [],
    "sync_interval_minutes": 30,
    "initial_sync_lookback_days": 30,
    "initial_sync_configured": False,
}


@dataclass(slots=True)
class _DeviceSession:
    client_id: str
    device_code: str
    user_code: str
    verification_uri: str
    interval: int


def _settings_value(
    field_values: dict[str, Any] | None,
    settings: dict[str, Any],
    key: str,
    default: Any = "",
) -> Any:
    if field_values and field_values.get(key) not in (None, ""):
        return field_values.get(key)
    return settings.get(key, default)


def _configured_client_id(field_values: dict[str, Any] | None, settings: dict[str, Any]) -> str:
    field_value = _settings_value(field_values, settings, "sources.github_activity.client_id")
    candidates = (
        field_value,
        settings.get("client_id"),
        os.environ.get(GITHUB_CLIENT_ID_ENV),
        DEFAULT_GITHUB_CLIENT_ID,
    )
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


class GitHubActivityPlugin(Plugin):
    """Registers local-only GitHub repository activity ingestion."""

    def __init__(self) -> None:
        super().__init__()
        self._device_sessions: dict[str, _DeviceSession] = {}

    def get_settings_actions(self) -> list[PluginSettingsActionSpec]:
        return [entry.model_copy(deep=True) for entry in self.manifest.settings_actions]

    async def start_settings_action(
        self,
        action_id: str,
        *,
        session_id: str,
        field_values: dict[str, Any] | None = None,
    ) -> PluginSettingsActionResult:
        if action_id != CONNECT_ACTION_ID:
            raise KeyError(action_id)
        settings = self._source_settings()
        client_id = _configured_client_id(field_values, settings)
        if not client_id:
            return PluginSettingsActionResult(
                status="failed",
                message=(
                    "GitHub authorization is not configured for this build. "
                    f"Set {GITHUB_CLIENT_ID_ENV} or package a GitHub OAuth client ID."
                ),
            )
        auth = GitHubDeviceAuthClient(client_id=client_id)
        try:
            device = auth.start()
        except GitHubClientError as exc:
            return PluginSettingsActionResult(status="failed", message=str(exc))
        self._device_sessions[session_id] = _DeviceSession(
            client_id=client_id,
            device_code=device.device_code,
            user_code=device.user_code,
            verification_uri=device.verification_uri,
            interval=device.interval,
        )
        return PluginSettingsActionResult(
            status="pending",
            message=f"Open {device.verification_uri} and enter code {device.user_code}.",
            data={
                "open_url": device.verification_uri,
                "verification_uri": device.verification_uri,
                "user_code": device.user_code,
                "interval": device.interval,
            },
        )

    async def poll_settings_action(
        self,
        action_id: str,
        *,
        session_id: str,
        field_values: dict[str, Any] | None = None,
    ) -> PluginSettingsActionResult:
        _ = field_values
        if action_id != CONNECT_ACTION_ID:
            raise KeyError(action_id)
        session = self._device_sessions.get(session_id)
        if session is None:
            return PluginSettingsActionResult(status="failed", message="GitHub connection session expired. Start again.")
        auth = GitHubDeviceAuthClient(client_id=session.client_id)
        try:
            token = auth.poll(session.device_code)
        except GitHubDeviceAuthorizationPending:
            return PluginSettingsActionResult(
                status="pending",
                message=f"Waiting for GitHub authorization. Enter {session.user_code} at {session.verification_uri}.",
                data={"verification_uri": session.verification_uri, "user_code": session.user_code},
            )
        except GitHubClientError as exc:
            self._device_sessions.pop(session_id, None)
            return PluginSettingsActionResult(status="failed", message=str(exc))

        self._device_sessions.pop(session_id, None)
        self.context.credentials.set("sources.github_activity.access_token", token.access_token)
        return PluginSettingsActionResult(
            status="succeeded",
            message="GitHub connected. Sync will run locally for the selected repositories.",
            settings_updates={
                "sources.github_activity.initial_sync_configured": True,
            },
        )

    async def cancel_settings_action(self, action_id: str, *, session_id: str) -> PluginSettingsActionResult:
        if action_id != CONNECT_ACTION_ID:
            raise KeyError(action_id)
        self._device_sessions.pop(session_id, None)
        return PluginSettingsActionResult(status="cancelled", message="GitHub connection cancelled.")

    def get_extraction_profiles(self) -> list[ExtractionProfileSpec]:
        return [
            ExtractionProfileSpec(
                profile_id="source.github_activity",
                source_types=["github_activity"],
                allowed_entity_types=["software", "person", "organization", "technology", "topic"],
                allowed_predicates=L2_PREDICATES,
                structured_allowed_entity_types=["software", "person", "organization", "technology", "topic"],
                structured_allowed_predicates=L2_PREDICATES,
                allowed_assertion_families=["project_profile"],
                allow_graph=True,
                allow_assertion=True,
                allowed_assertion_traits=["project.*"],
                derived_assertion_specs=[
                    {
                        "rule_id": "github_activity.recurring_project",
                        "source_predicates": ["WORKS_WITH", "COMMITTED"],
                        "source_types": ["github_activity"],
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
                    "These events are GitHub repository activity from repositories the user selected.\n"
                    "Treat them as external activity evidence, not user-authored profile claims.\n"
                    "Focus on repositories/projects, collaborators, pull requests, issues, commits, and CI status.\n"
                    "Do not infer durable preferences from one-off activity.\n\n"
                    "Assertion rules:\n"
                    "- Do not emit Phase 2 assertion candidates for GitHub events. Repeated\n"
                    "  WORKS_WITH or COMMITTED graph evidence may be aggregated later by the\n"
                    "  host-owned derived project rule declared in this profile."
                ),
            )
        ]

    def get_sources(self) -> list[tuple[str, object, SourceSpec]]:
        settings = self._source_settings()
        source_enabled = bool(settings.get("enabled", DEFAULT_SETTINGS["enabled"]))
        token = (self.context.credentials.get("sources.github_activity.access_token") or "") if source_enabled else ""
        repositories = list(settings.get("repositories") or []) if source_enabled else []
        source = GitHubActivitySource(
            access_token=token,
            repositories=[str(repo) for repo in repositories],
            initial_sync_lookback_days=int(settings.get("initial_sync_lookback_days") or 30),
        )
        sync_interval_minutes = int(settings.get("sync_interval_minutes") or DEFAULT_SETTINGS["sync_interval_minutes"])
        return [
            (
                "timeline.github_activity",
                source,
                SourceSpec(
                    source_id="timeline.github_activity",
                    display_name="GitHub Activity",
                    description="Local-only GitHub repository activity sync.",
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
                        "source_type": "github_activity",
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
        _ = summary_category, period_start, period_end, budget
        if source_type != "github_activity" or not events:
            return None
        repos: dict[str, int] = {}
        kinds: dict[str, int] = {}
        representative_event_ids: list[str] = []
        for event in events:
            provenance = _event_provenance(event)
            repo = str(provenance.get("repository") or "unknown")
            kind = str(provenance.get("event_kind") or "activity")
            repos[repo] = repos.get(repo, 0) + 1
            kinds[kind] = kinds.get(kind, 0) + 1
            event_id = str(event.get("event_id") or "").strip()
            if event_id and len(representative_event_ids) < 8:
                representative_event_ids.append(event_id)
        top_repos = sorted(repos.items(), key=lambda item: item[1], reverse=True)[:5]
        return {
            "feature_type": "github_activity",
            "event_count": len(events),
            "top_entities": [{"type": "repository", "name": repo, "count": count} for repo, count in top_repos],
            "top_activity_kinds": [{"kind": kind, "count": count} for kind, count in sorted(kinds.items())],
            "representative_event_ids": representative_event_ids,
            "summary_lines": [
                f"GitHub activity covered {len(events)} events across {len(repos)} repositories.",
                f"Most active repositories: {', '.join(repo for repo, _ in top_repos) or 'none'}.",
            ],
        }

    def _source_settings(self) -> dict[str, Any]:
        sources_settings = self.settings.get("sources", {})
        if not isinstance(sources_settings, dict):
            return dict(DEFAULT_SETTINGS)
        current = dict(DEFAULT_SETTINGS)
        raw = sources_settings.get("github_activity", {})
        if isinstance(raw, dict):
            current.update(raw)
        return current


def _event_provenance(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata_json")
    if not isinstance(metadata, dict):
        return {}
    activity_snapshot = metadata.get("activity_snapshot")
    if isinstance(activity_snapshot, dict):
        provenance = activity_snapshot.get("provenance")
        if isinstance(provenance, dict):
            return provenance
    return metadata
