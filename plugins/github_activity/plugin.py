"""GitHub Activity timeline plugin."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from magi_plugin_sdk import (
    ActivationFlowSpec,
    ContributionType,
    ExtensionFieldSpec,
    ExtractionProfileSpec,
    Plugin,
    PluginSettingsActionResult,
    PluginSettingsActionSpec,
    SensorSpec,
)

from .client import (
    GitHubClientError,
    GitHubDeviceAuthClient,
    GitHubDeviceAuthorizationPending,
)
from .sensor import GitHubActivitySensor


CONNECT_ACTION_ID = "connect_github"
L2_PREDICATES = ["WORKS_WITH", "COMMITTED", "USES", "REFERENCES"]
DEFAULT_SETTINGS = {
    "enabled": False,
    "client_id": "",
    "access_token": "",
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


def _activation_flow(prefix: str) -> ActivationFlowSpec:
    return ActivationFlowSpec(
        title="Connect GitHub",
        description=(
            "Choose the repositories Magi should read from this device. "
            "No GitHub data is sent to a Magi cloud service."
        ),
        confirm_label="Enable GitHub sync",
        cancel_label="Not now",
        enabled_key=f"{prefix}.enabled",
        configured_key=f"{prefix}.initial_sync_configured",
        fields=[
            ExtensionFieldSpec(
                key=f"{prefix}.client_id",
                type="input",
                label="GitHub Client ID",
                description="Client ID from the GitHub integration used for device authorization.",
                default="",
                required=True,
                section="connection",
                surface="timeline",
                order=10,
            ),
            ExtensionFieldSpec(
                key=f"{prefix}.repositories",
                type="tags",
                label="Repositories",
                description="Repositories to sync, for example owner/repo or a GitHub repository URL.",
                default=[],
                required=True,
                section="connection",
                surface="timeline",
                order=20,
            ),
            ExtensionFieldSpec(
                key=f"{prefix}.initial_sync_lookback_days",
                type="number",
                label="Initial Sync Days",
                description="How many recent days to import on the first sync.",
                default=30,
                min=1,
                max=365,
                section="connection",
                surface="timeline",
                order=30,
            ),
        ],
    )


def _fields(prefix: str) -> list[ExtensionFieldSpec]:
    return [
        ExtensionFieldSpec(
            key=f"{prefix}.enabled",
            type="switch",
            label="Enabled",
            description="Whether GitHub activity sync is active.",
            default=False,
            section="general",
            surface="timeline",
            order=10,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.client_id",
            type="input",
            label="GitHub Client ID",
            description="Client ID used for local device authorization.",
            default="",
            section="connection",
            surface="timeline",
            order=20,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.access_token",
            type="secret",
            label="Access Token",
            description="Stored locally after connecting GitHub. You can paste a token for testing.",
            default="",
            section="connection",
            surface="timeline",
            order=30,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.repositories",
            type="tags",
            label="Repositories",
            description="Repositories to sync, for example owner/repo.",
            default=[],
            section="connection",
            surface="timeline",
            order=40,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sync_interval_minutes",
            type="number",
            label="Sync Interval (minutes)",
            description="How often to check GitHub for updates.",
            default=30,
            min=5,
            max=1440,
            section="general",
            surface="timeline",
            order=50,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.initial_sync_lookback_days",
            type="number",
            label="Initial Sync Days",
            description="How many recent days to import when no previous cursor exists.",
            default=30,
            min=1,
            max=365,
            section="general",
            surface="timeline",
            order=60,
        ),
    ]


class GitHubActivityPlugin(Plugin):
    """Registers local-only GitHub repository activity ingestion."""

    def __init__(self) -> None:
        super().__init__()
        self._device_sessions: dict[str, _DeviceSession] = {}

    def get_settings_actions(self) -> list[PluginSettingsActionSpec]:
        return [
            PluginSettingsActionSpec(
                action_id=CONNECT_ACTION_ID,
                label="Connect GitHub",
                description="Open the GitHub device authorization page, enter the code, and save the resulting token locally.",
                button_label="Connect GitHub",
                presentation="inline",
                surface="timeline",
                contribution_id="timeline.github_activity",
                contribution_type=ContributionType.SENSOR,
                order=0,
                poll_interval_ms=5_000,
                timeout_ms=900_000,
                persist_settings_on_success=True,
                requires_enabled=False,
            )
        ]

    async def start_settings_action(
        self,
        action_id: str,
        *,
        session_id: str,
        field_values: dict[str, Any] | None = None,
    ) -> PluginSettingsActionResult:
        if action_id != CONNECT_ACTION_ID:
            raise KeyError(action_id)
        settings = self._sensor_settings()
        client_id = str(_settings_value(field_values, settings, "sensors.github_activity.client_id") or settings.get("client_id") or "").strip()
        if not client_id:
            return PluginSettingsActionResult(status="failed", message="Add a GitHub Client ID before connecting.")
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
        return PluginSettingsActionResult(
            status="succeeded",
            message="GitHub connected. Sync will run locally for the selected repositories.",
            settings_updates={
                "sensors.github_activity.access_token": token.access_token,
                "sensors.github_activity.initial_sync_configured": True,
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
                allow_graph=True,
                allow_assertion=False,
                assertion_mode="none",
                extraction_instructions=(
                    "These events are GitHub repository activity from repositories the user selected.\n"
                    "Treat them as external activity evidence, not user-authored profile claims.\n"
                    "Focus on repositories/projects, collaborators, pull requests, issues, commits, and CI status.\n"
                    "Do not infer durable preferences from one-off activity."
                ),
            )
        ]

    def get_sensors(self) -> list[tuple[str, object, SensorSpec]]:
        settings = self._sensor_settings()
        source_enabled = bool(settings.get("enabled", DEFAULT_SETTINGS["enabled"]))
        token = str(settings.get("access_token") or "").strip() if source_enabled else ""
        repositories = list(settings.get("repositories") or []) if source_enabled else []
        sensor = GitHubActivitySensor(
            access_token=token,
            repositories=[str(repo) for repo in repositories],
            initial_sync_lookback_days=int(settings.get("initial_sync_lookback_days") or 30),
        )
        sync_interval_minutes = int(settings.get("sync_interval_minutes") or DEFAULT_SETTINGS["sync_interval_minutes"])
        return [
            (
                "timeline.github_activity",
                sensor,
                SensorSpec(
                    sensor_id="timeline.github_activity",
                    display_name="GitHub Activity",
                    description="Local-only GitHub repository activity sync.",
                    domain="timeline",
                    surface="timeline",
                    sync_mode="interval",
                    polling_mode="interval",
                    fields=_fields("sensors.github_activity"),
                    metadata={
                        "source_type": "github_activity",
                        "default_settings": dict(DEFAULT_SETTINGS),
                        "activation_flow": _activation_flow("sensors.github_activity").model_dump(),
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

    def _sensor_settings(self) -> dict[str, Any]:
        sensors_settings = self.settings.get("sensors", {})
        if not isinstance(sensors_settings, dict):
            return dict(DEFAULT_SETTINGS)
        current = dict(DEFAULT_SETTINGS)
        raw = sensors_settings.get("github_activity", {})
        if isinstance(raw, dict):
            current.update(raw)
        return current


def _event_provenance(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata_json")
    if not isinstance(metadata, dict):
        return {}
    timeline = metadata.get("timeline")
    if isinstance(timeline, dict):
        provenance = timeline.get("provenance")
        if isinstance(provenance, dict):
            return provenance
    return metadata
