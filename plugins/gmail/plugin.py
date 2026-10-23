"""Connection-owned Gmail setup and declarative mail source registration."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from magi_plugin_sdk import Plugin, PluginSettingsActionResult, PluginSettingsActionSpec
from magi_plugin_sdk.source_query import SourceQueryCapabilities
from magi_plugin_sdk.sources import SourceSpec
from mail_core import MailSource

from .auth import OAuthSession
from .client import API_ROOT, SECRET_KEY, TOKEN_KEY, GmailClient, GmailError
from .provider import GmailProvider


class GmailPlugin(Plugin):
    def __init__(self) -> None:
        super().__init__()
        self._sessions: dict[str, OAuthSession] = {}
        self._client: GmailClient | None = None
        self._poll_lock = asyncio.Lock()

    def _settings(self) -> dict:
        return dict(self.settings.get("sources", {}).get("mail", {}))

    def _get_client(self) -> GmailClient:
        if self._client is None:
            self._client = GmailClient(self.context.credentials)
        return self._client

    def get_settings_actions(self) -> list[PluginSettingsActionSpec]:
        return [entry.model_copy(deep=True) for entry in self.manifest.settings_actions]

    async def start_settings_action(self, action_id: str, *, session_id: str,
                                    field_values: dict[str, Any] | None = None) -> PluginSettingsActionResult:
        if action_id == "disconnect":
            for session in self._sessions.values():
                await session.close()
            self._sessions.clear()
            self.context.credentials.delete(TOKEN_KEY)
            if self._client:
                await self._client.close()
                self._client = None
            return PluginSettingsActionResult(message=self.t("gmail.disconnected", fallback="Gmail disconnected on this device."),
                settings_updates={"sources.mail.enabled": False, "sources.mail.initial_sync_configured": False})
        if action_id != "connect":
            raise KeyError(action_id)
        values = field_values or {}
        client_id = str(values.get("sources.mail.client_id") or self._settings().get("client_id") or "").strip()
        if not client_id.endswith(".apps.googleusercontent.com"):
            return PluginSettingsActionResult(status="failed", message=self.t("gmail.client_required", fallback="Enter a Google Desktop OAuth client ID first."))
        secret = str(values.get(SECRET_KEY) or (self.context.credentials.get(SECRET_KEY) if client_id == self._settings().get("client_id") else "") or "")
        # One active authorization per connection, with no cross-account global state.
        for session in self._sessions.values():
            await session.close()
        self._sessions.clear()
        session = OAuthSession(client_id, secret)
        url = await session.start()
        self._sessions[session_id] = session
        return PluginSettingsActionResult(status="pending", message=self.t("gmail.authorize", fallback="Authorize Gmail in the browser on the service host, then return here."),
            data={"open_url": url})

    async def poll_settings_action(self, action_id: str, *, session_id: str,
                                   field_values: dict[str, Any] | None = None) -> PluginSettingsActionResult:
        async with self._poll_lock:
            return await self._finish_authorization(action_id, session_id=session_id)

    async def _finish_authorization(self, action_id: str, *, session_id: str) -> PluginSettingsActionResult:
        if action_id != "connect":
            raise KeyError(action_id)
        session = self._sessions.get(session_id)
        if session is None:
            return PluginSettingsActionResult(status="failed", message=self.t("gmail.expired", fallback="Authorization expired. Start again."))
        if not session.code and not session.error:
            return PluginSettingsActionResult(status="pending", message=self.t("gmail.waiting", fallback="Waiting for Gmail authorization."))
        try:
            token = await session.exchange(self._get_client())
            profile = await self._get_client().request_json("GET", API_ROOT + "/profile",
                headers={"Authorization": "Bearer " + token["access_token"]})
            email = str(profile["emailAddress"])
            existing = str(self._settings().get("account_email") or "")
            if existing and existing.casefold() != email.casefold():
                return PluginSettingsActionResult(status="failed", message=self.t("gmail.different_account", fallback="Create a new connection for a different Gmail account."))
            if self._sessions.get(session_id) is not session:
                return PluginSettingsActionResult(status="cancelled", message=self.t("gmail.cancelled", fallback="Gmail authorization cancelled."))
            token["email"] = email
            if session.client_secret:
                self.context.credentials.set(SECRET_KEY, session.client_secret)
            self.context.credentials.set(TOKEN_KEY, json.dumps(token))
            return PluginSettingsActionResult(message=self.t("gmail.connected", fallback="Gmail connected with read-only access."),
                settings_updates={"sources.mail.account_email": email, "sources.mail.client_id": session.client_id,
                                  "sources.mail.initial_sync_configured": True})
        except (GmailError, PermissionError, ValueError, TypeError, KeyError, httpx.HTTPError):
            return PluginSettingsActionResult(status="failed", message=self.t("gmail.failed", fallback="Gmail authorization failed. Check the OAuth configuration and retry."))
        finally:
            await session.close()
            self._sessions.pop(session_id, None)

    async def cancel_settings_action(self, action_id: str, *, session_id: str) -> PluginSettingsActionResult:
        session = self._sessions.pop(session_id, None)
        if session:
            await session.close()
        return PluginSettingsActionResult(status="cancelled", message=self.t("gmail.cancelled", fallback="Gmail authorization cancelled."))

    def get_sources(self) -> list[tuple[str, MailSource, SourceSpec]]:
        settings = self._settings()
        source = MailSource(GmailProvider(self._get_client(), lookback_days=int(settings.get("initial_sync_lookback_days") or 30),
                                          account_email=str(settings.get("account_email") or "")))
        return [("mail", source, SourceSpec(source_id="mail", display_name="Gmail", domain="mail",
            description=self.t("gmail.source_description", fallback="Read-only Gmail messages and thread evidence."),
            surface="timeline", sync_mode="interval", polling_mode="interval",
            query_capabilities=SourceQueryCapabilities(provider="gmail", operations=["search", "read", "thread"],
                fields=["content", "labels", "thread", "attachments"], max_page_size=50),
            fields=[field.model_copy(deep=True) for field in sorted(self.manifest.settings_fields, key=lambda field: field.order)
                    if field.surface == "timeline" and field.section != "activation"],
            metadata={"source_type": "mail", "sync_interval_minutes": int(settings.get("sync_interval_minutes") or 10),
                "default_settings": {field.key.rsplit(".", 1)[-1]: field.model_copy(deep=True).default
                    for field in self.manifest.settings_fields if field.key.startswith("sources.") and field.type != "secret"},
                "activation_flow": self.manifest.activation_flow.model_dump() if self.manifest.activation_flow else None},
        ))]

    async def shutdown(self) -> None:
        for session in self._sessions.values():
            await session.close()
        self._sessions.clear()
        if self._client:
            await self._client.close()
            self._client = None
