"""Weixin channel plugin entrypoint."""

from __future__ import annotations

from base64 import b64encode
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from magi_plugin_sdk import (
    ContributionType,
    ExtensionFieldSpec,
    Plugin,
    PluginSettingsActionResult,
    PluginSettingsActionSpec,
    PluginSettingsResourceSpec,
)
from magi_plugin_sdk.channels import Channel

from .adapter import WeixinChannel, WeixinChannelConfig
from .api import (
    DEFAULT_BASE_URL,
    DEFAULT_BOT_TYPE,
    DEFAULT_CDN_BASE_URL,
    DEFAULT_LONG_POLL_TIMEOUT_MS,
    WeixinApiError,
    WeixinApiClient,
    WeixinApiTimeout,
)
from .auth import DEFAULT_LOGIN_TIMEOUT_MS, MAX_QR_REFRESH_COUNT
from .state import WeixinCredentials, WeixinStateStore


QR_LOGIN_ACTION_ID = "qr_login"
VALIDATE_CREDENTIALS_ACTION_ID = "validate_credentials"
RESET_CURSOR_ACTION_ID = "reset_cursor"
CLEAR_PROCESSED_MESSAGES_ACTION_ID = "clear_processed_messages"
LOGOUT_ACTION_ID = "logout"
CHANNEL_STATUS_RESOURCE_NAME = "channel_status"
ACCOUNTS_RESOURCE_NAME = "accounts"
QR_STATUS_POLL_TIMEOUT_MS = 8_000
VALIDATE_CREDENTIALS_TIMEOUT_MS = 3_000


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


def _weixin_metadata(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata_json")
    if not isinstance(metadata, dict):
        return {}
    if metadata.get("external_chat_id") or metadata.get("channel_type") == "weixin":
        return metadata
    channel = metadata.get("channel")
    if isinstance(channel, dict):
        return channel
    activity_snapshot = metadata.get("activity_snapshot")
    if isinstance(activity_snapshot, dict):
        provenance = activity_snapshot.get("provenance")
        if isinstance(provenance, dict):
            return provenance
    return metadata


@dataclass(slots=True)
class _QrLoginSession:
    qrcode: str
    qr_code_url: str
    qr_code_image_url: str
    base_url: str
    current_base_url: str
    bot_type: str
    state_dir: str
    channel_version: str
    ilink_app_id: str
    route_tag: str
    refresh_count: int = 1


def _settings_str(
    field_values: dict[str, Any] | None,
    settings: dict[str, Any],
    key: str,
    default: str,
) -> str:
    if field_values and field_values.get(key) not in (None, ""):
        return str(field_values.get(key) or default).strip() or default
    return str(settings.get(key) or default).strip() or default


def _qr_code_data_url(content: str) -> str:
    try:
        import segno
    except ImportError as exc:
        raise RuntimeError("The Weixin QR login action requires the 'segno' package.") from exc

    output = BytesIO()
    segno.make(content, error="m").save(output, kind="svg", scale=8, border=2, xmldecl=False)
    encoded = b64encode(output.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def _qr_pending_result(session: _QrLoginSession, message: str, status: str) -> PluginSettingsActionResult:
    return PluginSettingsActionResult(
        status="pending",
        message=message,
        data={
            "qr_code_url": session.qr_code_image_url,
            "qr_code_text": session.qr_code_url,
            "qr_code_link": session.qr_code_url,
            "qrcode": session.qrcode,
            "status": status,
        },
    )


class WeixinPlugin(Plugin):
    """Weixin direct-message channel plugin."""

    def __init__(self) -> None:
        super().__init__()
        self._qr_login_sessions: dict[str, _QrLoginSession] = {}

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
        _ = summary_category, period_start, period_end
        if source_type != "weixin" or not events:
            return None

        chat_counter: Counter[str] = Counter()
        representative_event_ids: list[str] = []
        for event in events:
            channel_metadata = _weixin_metadata(event)
            chat_id = str(channel_metadata.get("external_chat_id") or "unknown").strip() or "unknown"
            chat_counter[chat_id] += 1
            event_id = str(event.get("event_id") or "").strip()
            if event_id and len(representative_event_ids) < 8:
                representative_event_ids.append(event_id)

        covered_event_count = len(events)
        total_event_count = _budget_int(budget, "total_event_count", covered_event_count)
        omitted_event_count = max(0, total_event_count - covered_event_count)
        summary_lines = [
            f"Weixin feature coverage used {covered_event_count} messages across {len(chat_counter)} direct chats."
        ]

        if omitted_event_count > 0:
            summary_lines.append(
                f"Weixin feature coverage used {covered_event_count} representative messages; {omitted_event_count} additional messages were compacted."
            )

        return {
            "feature_type": "weixin_channel",
            "message_count": covered_event_count,
            "total_event_count": total_event_count,
            "covered_event_count": covered_event_count,
            "omitted_event_count": omitted_event_count,
            "coverage_ratio": (covered_event_count / total_event_count) if total_event_count else None,
            "chat_count": len(chat_counter),
            "representative_event_ids": representative_event_ids,
            "summary_lines": summary_lines,
        }

    def get_channel(self) -> Channel:
        manifest_version = self.manifest.version if self.manifest is not None else "0.1.0"
        config = WeixinChannelConfig(
            bot_token=self.settings.get("bot_token", ""),
            account_id=self.settings.get("account_id", ""),
            credentials_path=self.settings.get("credentials_path", ""),
            state_dir=self.settings.get("state_dir", "~/.magi/weixin"),
            base_url=self.settings.get("base_url", DEFAULT_BASE_URL),
            cdn_base_url=self.settings.get("cdn_base_url", DEFAULT_CDN_BASE_URL),
            bot_type=self.settings.get("bot_type", DEFAULT_BOT_TYPE),
            ilink_app_id=self.settings.get("ilink_app_id", "bot"),
            route_tag=str(self.settings.get("route_tag", "") or ""),
            allowed_user_ids=list(self.settings.get("allowed_user_ids") or []),
            max_message_length=int(self.settings.get("max_message_length", 4000)),
            poll_timeout_ms=int(self.settings.get("poll_timeout_ms", DEFAULT_LONG_POLL_TIMEOUT_MS)),
            request_timeout_ms=int(self.settings.get("request_timeout_ms", 15_000)),
            enable_typing_indicator=bool(self.settings.get("enable_typing_indicator", True)),
            channel_version=manifest_version,
        )
        return WeixinChannel(config=config)

    def get_settings_actions(self) -> list[PluginSettingsActionSpec]:
        return [
            PluginSettingsActionSpec(
                action_id=QR_LOGIN_ACTION_ID,
                label="Weixin QR Login",
                description="Scan with Weixin to authorize this channel without pasting a bot token manually.",
                button_label="Start QR Login",
                presentation="qr_code",
                surface="extensions",
                contribution_type=ContributionType.CHANNEL,
                order=0,
                poll_interval_ms=2_000,
                timeout_ms=DEFAULT_LOGIN_TIMEOUT_MS,
                persist_settings_on_success=True,
            ),
            PluginSettingsActionSpec(
                action_id=VALIDATE_CREDENTIALS_ACTION_ID,
                label="Validate Credentials",
                description="Check the saved Weixin credentials and gateway connection.",
                button_label="Test Connection",
                presentation="inline",
                surface="extensions",
                contribution_type=ContributionType.CHANNEL,
                order=1,
                poll_interval_ms=2_000,
                timeout_ms=30_000,
                persist_settings_on_success=False,
            ),
            PluginSettingsActionSpec(
                action_id=RESET_CURSOR_ACTION_ID,
                label="Reset Cursor",
                description="Clear the saved Weixin getUpdates cursor and reconnect from the gateway's next position.",
                button_label="Reset Cursor",
                presentation="inline",
                surface="extensions",
                contribution_type=ContributionType.CHANNEL,
                order=2,
                poll_interval_ms=2_000,
                timeout_ms=30_000,
                persist_settings_on_success=False,
            ),
            PluginSettingsActionSpec(
                action_id=CLEAR_PROCESSED_MESSAGES_ACTION_ID,
                label="Clear Message Dedupe",
                description="Clear the local processed-message cache used for Weixin retry safety.",
                button_label="Clear Dedupe",
                presentation="inline",
                surface="extensions",
                contribution_type=ContributionType.CHANNEL,
                order=3,
                poll_interval_ms=2_000,
                timeout_ms=30_000,
                persist_settings_on_success=False,
            ),
            PluginSettingsActionSpec(
                action_id=LOGOUT_ACTION_ID,
                label="Logout Weixin",
                description="Remove saved Weixin credentials for this account and stop the channel until QR login runs again.",
                button_label="Logout",
                presentation="inline",
                surface="extensions",
                contribution_type=ContributionType.CHANNEL,
                order=4,
                destructive=True,
                poll_interval_ms=2_000,
                timeout_ms=30_000,
                persist_settings_on_success=True,
            ),
        ]

    def get_settings_resources(self) -> list[PluginSettingsResourceSpec]:
        return [
            PluginSettingsResourceSpec(
                resource_name=CHANNEL_STATUS_RESOURCE_NAME,
                resource_type="channel_status",
                description="Latest Weixin channel runtime status.",
            ),
            PluginSettingsResourceSpec(
                resource_name=ACCOUNTS_RESOURCE_NAME,
                resource_type="collection",
                description="Saved Weixin accounts in the configured state directory.",
            )
        ]

    def read_settings_resource(self, resource_name: str) -> Any:
        state_dir = str(self.settings.get("state_dir") or "~/.magi/weixin")
        store = WeixinStateStore(state_dir)
        if resource_name == ACCOUNTS_RESOURCE_NAME:
            return {
                "groups": [
                    {
                        "group_id": "accounts",
                        "label": "Accounts",
                        "items": [
                            {"item_id": account_id, "label": account_id}
                            for account_id in store.list_account_ids()
                        ],
                    }
                ]
            }
        if resource_name != CHANNEL_STATUS_RESOURCE_NAME:
            raise KeyError(resource_name)
        status = store.load_channel_status()
        status.setdefault("state", "stopped")
        status.setdefault("running", False)
        status.setdefault("configured", bool(self.settings.get("account_id") or self.settings.get("credentials_path")))
        return status

    async def start_settings_action(
        self,
        action_id: str,
        *,
        session_id: str,
        field_values: dict[str, Any] | None = None,
    ) -> PluginSettingsActionResult:
        if action_id != QR_LOGIN_ACTION_ID:
            if action_id == VALIDATE_CREDENTIALS_ACTION_ID:
                return await self._validate_credentials(field_values)
            if action_id == RESET_CURSOR_ACTION_ID:
                return self._reset_cursor(field_values)
            if action_id == CLEAR_PROCESSED_MESSAGES_ACTION_ID:
                return self._clear_processed_messages(field_values)
            if action_id == LOGOUT_ACTION_ID:
                return self._logout(field_values)
            raise KeyError(action_id)

        session = await self._create_qr_login_session(field_values)
        self._qr_login_sessions[session_id] = session
        return _qr_pending_result(session, self.t("action_messages.scan"), "waiting")

    async def poll_settings_action(
        self,
        action_id: str,
        *,
        session_id: str,
        field_values: dict[str, Any] | None = None,
    ) -> PluginSettingsActionResult:
        _ = field_values
        if action_id != QR_LOGIN_ACTION_ID:
            raise KeyError(action_id)

        session = self._qr_login_sessions.get(session_id)
        if session is None:
            return PluginSettingsActionResult(status="failed", message=self.t("action_messages.session_missing"))

        client = self._qr_api_client(session.current_base_url, session)
        try:
            status = await client.get_qr_status(qrcode=session.qrcode, timeout_ms=QR_STATUS_POLL_TIMEOUT_MS)
        except WeixinApiTimeout:
            return _qr_pending_result(session, self.t("action_messages.waiting"), "waiting")
        status_name = str(status.get("status") or "wait")
        if status_name == "wait":
            return _qr_pending_result(session, self.t("action_messages.waiting"), "waiting")
        if status_name == "scaned":
            return _qr_pending_result(session, self.t("action_messages.scanned"), "scanned")
        if status_name == "scaned_but_redirect":
            redirect_host = str(status.get("redirect_host") or "").strip()
            if redirect_host:
                session.current_base_url = f"https://{redirect_host}"
            return _qr_pending_result(session, self.t("action_messages.redirecting"), "redirecting")
        if status_name == "expired":
            return await self._refresh_qr_login_session(session)
        if status_name == "confirmed":
            return self._finish_qr_login_session(session_id, session, status)
        return PluginSettingsActionResult(
            status="failed",
            message=self.t("action_messages.unexpected_status", status=status_name),
        )

    async def cancel_settings_action(
        self,
        action_id: str,
        *,
        session_id: str,
    ) -> PluginSettingsActionResult:
        if action_id != QR_LOGIN_ACTION_ID:
            raise KeyError(action_id)
        self._qr_login_sessions.pop(session_id, None)
        return PluginSettingsActionResult(status="cancelled", message=self.t("action_messages.cancelled"))

    async def _create_qr_login_session(self, field_values: dict[str, Any] | None) -> _QrLoginSession:
        manifest_version = self.manifest.version if self.manifest is not None else "0.1.0"
        base_url = _settings_str(field_values, self.settings, "base_url", DEFAULT_BASE_URL)
        bot_type = _settings_str(field_values, self.settings, "bot_type", DEFAULT_BOT_TYPE)
        state_dir = _settings_str(field_values, self.settings, "state_dir", "~/.magi/weixin")
        ilink_app_id = _settings_str(field_values, self.settings, "ilink_app_id", "bot")
        route_tag = _settings_str(field_values, self.settings, "route_tag", "")
        client = WeixinApiClient(
            base_url=base_url,
            channel_version=manifest_version,
            ilink_app_id=ilink_app_id,
            route_tag=route_tag,
        )
        qrcode_payload = await client.get_qr_code(bot_type=bot_type)
        qrcode = str(qrcode_payload.get("qrcode") or "")
        qr_code_url = str(qrcode_payload.get("qrcode_img_content") or "")
        if not qrcode or not qr_code_url:
            raise RuntimeError(self.t("action_messages.no_qr"))
        qr_code_image_url = _qr_code_data_url(qr_code_url)
        return _QrLoginSession(
            qrcode=qrcode,
            qr_code_url=qr_code_url,
            qr_code_image_url=qr_code_image_url,
            base_url=base_url,
            current_base_url=base_url,
            bot_type=bot_type,
            state_dir=state_dir,
            channel_version=manifest_version,
            ilink_app_id=ilink_app_id,
            route_tag=route_tag,
        )

    async def _validate_credentials(self, field_values: dict[str, Any] | None) -> PluginSettingsActionResult:
        manifest_version = self.manifest.version if self.manifest is not None else "0.1.0"
        base_url = _settings_str(field_values, self.settings, "base_url", DEFAULT_BASE_URL)
        state_dir = _settings_str(field_values, self.settings, "state_dir", "~/.magi/weixin")
        account_id = _settings_str(field_values, self.settings, "account_id", "")
        bot_token = _settings_str(field_values, self.settings, "bot_token", "")
        credentials_path = _settings_str(field_values, self.settings, "credentials_path", "")
        ilink_app_id = _settings_str(field_values, self.settings, "ilink_app_id", "bot")
        route_tag = _settings_str(field_values, self.settings, "route_tag", "")
        store = WeixinStateStore(state_dir)

        if bot_token.strip() and account_id.strip():
            credentials = WeixinCredentials(account_id=account_id.strip(), token=bot_token.strip(), base_url=base_url)
        else:
            credentials = store.load_credentials(account_id=account_id, credentials_path=credentials_path)

        if credentials is None:
            return PluginSettingsActionResult(status="failed", message=self.t("action_messages.validate_missing_credentials"))

        client = WeixinApiClient(
            base_url=credentials.base_url or base_url,
            token=credentials.token,
            channel_version=manifest_version,
            ilink_app_id=ilink_app_id,
            route_tag=route_tag,
        )
        try:
            response = await client.get_updates(
                get_updates_buf=store.load_sync_buf(credentials.account_id),
                timeout_ms=VALIDATE_CREDENTIALS_TIMEOUT_MS,
            )
        except (WeixinApiError, WeixinApiTimeout) as exc:
            return PluginSettingsActionResult(
                status="failed",
                message=self.t("action_messages.validate_failed", error=str(exc)),
                data={"account_id": credentials.account_id},
            )

        ret = response.get("ret")
        errcode = response.get("errcode")
        if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
            return PluginSettingsActionResult(
                status="failed",
                message=self.t("action_messages.validate_failed", error=str(response)),
                data={"account_id": credentials.account_id, "response": response},
            )
        return PluginSettingsActionResult(
            status="succeeded",
            message=self.t("action_messages.validate_succeeded"),
            data={"account_id": credentials.account_id, "base_url": client.base_url},
        )

    def _resolve_action_credentials(
        self,
        field_values: dict[str, Any] | None,
    ) -> tuple[WeixinStateStore, WeixinCredentials | None, str]:
        state_dir = _settings_str(field_values, self.settings, "state_dir", "~/.magi/weixin")
        account_id = _settings_str(field_values, self.settings, "account_id", "")
        credentials_path = _settings_str(field_values, self.settings, "credentials_path", "")
        store = WeixinStateStore(state_dir)
        return store, store.load_credentials(account_id=account_id, credentials_path=credentials_path), credentials_path

    def _reset_cursor(self, field_values: dict[str, Any] | None) -> PluginSettingsActionResult:
        store, credentials, _ = self._resolve_action_credentials(field_values)
        if credentials is None:
            return PluginSettingsActionResult(status="failed", message=self.t("action_messages.validate_missing_credentials"))
        store.clear_sync_buf(credentials.account_id)
        return PluginSettingsActionResult(
            status="succeeded",
            message=self.t("action_messages.reset_cursor_succeeded"),
            data={"account_id": credentials.account_id, "refresh_channels": True},
        )

    def _clear_processed_messages(self, field_values: dict[str, Any] | None) -> PluginSettingsActionResult:
        store, credentials, _ = self._resolve_action_credentials(field_values)
        if credentials is None:
            return PluginSettingsActionResult(status="failed", message=self.t("action_messages.validate_missing_credentials"))
        store.clear_processed_message_ids(credentials.account_id)
        return PluginSettingsActionResult(
            status="succeeded",
            message=self.t("action_messages.clear_processed_succeeded"),
            data={"account_id": credentials.account_id, "refresh_channels": True},
        )

    def _logout(self, field_values: dict[str, Any] | None) -> PluginSettingsActionResult:
        store, credentials, credentials_path = self._resolve_action_credentials(field_values)
        if credentials is None:
            return PluginSettingsActionResult(status="failed", message=self.t("action_messages.validate_missing_credentials"))
        store.delete_credentials(credentials.account_id, credentials_path=credentials_path)
        store.update_channel_status(state="unconfigured", running=False, configured=False, account_id="", last_error="")
        return PluginSettingsActionResult(
            status="succeeded",
            message=self.t("action_messages.logout_succeeded"),
            data={"account_id": credentials.account_id, "refresh_channels": True},
            settings_updates={"account_id": "", "credentials_path": "", "bot_token": ""},
        )

    async def _refresh_qr_login_session(self, session: _QrLoginSession) -> PluginSettingsActionResult:
        session.refresh_count += 1
        if session.refresh_count > MAX_QR_REFRESH_COUNT:
            return PluginSettingsActionResult(status="failed", message=self.t("action_messages.expired_too_many"))
        payload = await self._qr_api_client(session.base_url, session).get_qr_code(bot_type=session.bot_type)
        session.qrcode = str(payload.get("qrcode") or "")
        session.qr_code_url = str(payload.get("qrcode_img_content") or "")
        session.qr_code_image_url = _qr_code_data_url(session.qr_code_url) if session.qr_code_url else ""
        session.current_base_url = session.base_url
        if not session.qrcode or not session.qr_code_url or not session.qr_code_image_url:
            return PluginSettingsActionResult(status="failed", message=self.t("action_messages.refresh_failed"))
        return _qr_pending_result(session, self.t("action_messages.refreshed"), "refreshed")

    def _finish_qr_login_session(
        self,
        session_id: str,
        session: _QrLoginSession,
        status: dict[str, Any],
    ) -> PluginSettingsActionResult:
        token = str(status.get("bot_token") or "").strip()
        account_id = str(status.get("ilink_bot_id") or "").strip()
        if not token or not account_id:
            return PluginSettingsActionResult(status="failed", message=self.t("action_messages.missing_credentials"))

        credentials = WeixinCredentials(
            account_id=account_id,
            token=token,
            base_url=str(status.get("baseurl") or session.current_base_url or DEFAULT_BASE_URL),
            user_id=str(status.get("ilink_user_id") or ""),
        )
        saved_path = WeixinStateStore(session.state_dir).save_credentials(credentials)
        self._qr_login_sessions.pop(session_id, None)
        return PluginSettingsActionResult(
            status="succeeded",
            message=self.t("action_messages.succeeded"),
            data={"account_id": account_id, "credentials_path": str(saved_path)},
            settings_updates={
                "account_id": account_id,
                "credentials_path": str(saved_path),
                "state_dir": session.state_dir,
                "base_url": credentials.base_url,
                "bot_token": "",
            },
        )

    @staticmethod
    def _qr_api_client(base_url: str, session: _QrLoginSession) -> WeixinApiClient:
        return WeixinApiClient(
            base_url=base_url,
            channel_version=session.channel_version,
            ilink_app_id=session.ilink_app_id,
            route_tag=session.route_tag,
        )

    def get_channel_fields(self) -> list[ExtensionFieldSpec]:
        return [
            ExtensionFieldSpec(
                key="bot_token",
                type="secret",
                label="Bot Token",
                description="iLink bot token returned by the Weixin QR login flow. Leave empty when using a credentials file.",
                default="",
                surface="extensions",
                order=0,
            ),
            ExtensionFieldSpec(
                key="account_id",
                type="input",
                label="Account ID",
                description="iLink bot account ID. Leave empty only when the state directory contains exactly one logged-in account.",
                default="",
                placeholder="example@im.bot",
                surface="extensions",
                order=1,
            ),
            ExtensionFieldSpec(
                key="credentials_path",
                type="path",
                label="Credentials File",
                description="Optional JSON credentials file with token, account_id, base_url, and user_id fields.",
                default="",
                placeholder="~/.magi/weixin/accounts/example@im.bot.json",
                surface="extensions",
                order=2,
            ),
            ExtensionFieldSpec(
                key="state_dir",
                type="path",
                label="State Directory",
                description="Directory used for QR-login credentials, getUpdates cursor, and context tokens.",
                default="~/.magi/weixin",
                surface="extensions",
                order=3,
            ),
            ExtensionFieldSpec(
                key="allowed_user_ids",
                type="tags",
                label="Allowed Weixin User IDs",
                description="Whitelist of Weixin user IDs. Empty means allow all users who can message this bot.",
                default=[],
                surface="extensions",
                order=4,
            ),
            ExtensionFieldSpec(
                key="enable_typing_indicator",
                type="switch",
                label="Typing Indicator",
                description="Send Weixin typing status while Magi is processing a message.",
                default=True,
                surface="extensions",
                order=5,
            ),
            ExtensionFieldSpec(
                key="base_url",
                type="input",
                label="API Base URL",
                description="Weixin iLink API base URL.",
                default=DEFAULT_BASE_URL,
                surface="extensions",
                order=6,
            ),
            ExtensionFieldSpec(
                key="bot_type",
                type="input",
                label="Bot Type",
                description="iLink bot_type used by the QR login helper.",
                default=DEFAULT_BOT_TYPE,
                surface="extensions",
                order=7,
            ),
            ExtensionFieldSpec(
                key="route_tag",
                type="input",
                label="Route Tag",
                description="Optional SKRouteTag header for internal routing.",
                default="",
                surface="extensions",
                order=8,
            ),
            ExtensionFieldSpec(
                key="max_message_length",
                type="number",
                label="Max Message Length",
                description="Maximum characters per Weixin outbound text message.",
                default=4000,
                surface="extensions",
                order=9,
            ),
            ExtensionFieldSpec(
                key="poll_timeout_ms",
                type="number",
                label="Poll Timeout",
                description="Long-poll timeout in milliseconds for getUpdates.",
                default=DEFAULT_LONG_POLL_TIMEOUT_MS,
                surface="extensions",
                order=10,
            ),
        ]
