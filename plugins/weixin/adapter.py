"""Weixin channel adapter for Magi."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from magi_plugin_sdk import get_logger
from magi_plugin_sdk.channels import (
    Channel,
    ChannelMessageDispatcherProtocol,
    ChannelSessionMapperProtocol,
    ChannelTarget,
    OutboundContent,
)

from .api import (
    DEFAULT_API_TIMEOUT_MS,
    DEFAULT_BASE_URL,
    DEFAULT_BOT_TYPE,
    DEFAULT_CDN_BASE_URL,
    DEFAULT_LONG_POLL_TIMEOUT_MS,
    MESSAGE_TYPE_BOT,
    SESSION_EXPIRED_ERRCODE,
    WeixinApiClient,
)
from .normalizers import body_from_item_list
from .state import WeixinCredentials, WeixinStateStore

logger = get_logger(__name__)


@dataclass(slots=True)
class WeixinChannelConfig:
    """Runtime settings for the Weixin channel."""

    bot_token: str = ""
    account_id: str = ""
    credentials_path: str = ""
    state_dir: str = "~/.magi/weixin"
    base_url: str = DEFAULT_BASE_URL
    cdn_base_url: str = DEFAULT_CDN_BASE_URL
    bot_type: str = DEFAULT_BOT_TYPE
    ilink_app_id: str = "bot"
    route_tag: str = ""
    allowed_user_ids: list[str] = field(default_factory=list)
    max_message_length: int = 4000
    poll_timeout_ms: int = DEFAULT_LONG_POLL_TIMEOUT_MS
    request_timeout_ms: int = DEFAULT_API_TIMEOUT_MS
    session_expired_pause_ms: int = 60 * 60 * 1000
    enable_typing_indicator: bool = True
    channel_version: str = "0.1.0"


@dataclass(slots=True)
class _CachedTypingTicket:
    ticket: str
    next_refresh_at_ms: int


class WeixinChannel(Channel):
    """Bidirectional text channel backed by the Weixin iLink bot gateway."""

    def __init__(
        self,
        *,
        config: WeixinChannelConfig,
        session_mapper: ChannelSessionMapperProtocol | None = None,
        message_dispatcher: ChannelMessageDispatcherProtocol | None = None,
    ) -> None:
        self._config = config
        self._session_mapper = session_mapper
        self._message_dispatcher = message_dispatcher
        self._state = WeixinStateStore(config.state_dir)
        self._credentials: WeixinCredentials | None = None
        self._api: WeixinApiClient | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._context_tokens: dict[str, str] = {}
        self._typing_cache: dict[str, _CachedTypingTicket] = {}

    @property
    def channel_type(self) -> str:
        return "weixin"

    def bind_session_mapper(self, session_mapper: ChannelSessionMapperProtocol) -> None:
        self._session_mapper = session_mapper

    def bind_message_dispatcher(self, dispatcher: ChannelMessageDispatcherProtocol) -> None:
        self._message_dispatcher = dispatcher

    async def start(self) -> None:
        credentials = self._load_credentials()
        if credentials is None:
            raise ValueError(
                "Weixin credentials are required. Run plugins/weixin/login.py or configure bot_token and account_id."
            )
        self._credentials = credentials
        self._api = WeixinApiClient(
            base_url=credentials.base_url or self._config.base_url or DEFAULT_BASE_URL,
            token=credentials.token,
            channel_version=self._config.channel_version,
            ilink_app_id=self._config.ilink_app_id,
            route_tag=self._config.route_tag,
        )
        self._context_tokens = self._state.load_context_tokens(credentials.account_id)
        self._stop_event = asyncio.Event()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("Weixin channel started", account_id=credentials.account_id)

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        logger.info("Weixin channel stopped")

    async def send_message(self, target: ChannelTarget, content: OutboundContent) -> None:
        if self._api is None or self._credentials is None:
            raise RuntimeError("Weixin channel is not started")
        text = (content.text or "").strip()
        if not text:
            return
        if len(text) > self._config.max_message_length:
            text = text[: self._config.max_message_length - 3] + "..."
        context_token = self._context_tokens.get(target.external_chat_id)
        await self._api.send_text_message(
            to_user_id=target.external_chat_id,
            text=text,
            context_token=context_token,
            timeout_ms=self._config.request_timeout_ms,
        )

    async def send_typing_indicator(self, target: ChannelTarget) -> None:
        if not self._config.enable_typing_indicator or self._api is None:
            return
        try:
            context_token = self._context_tokens.get(target.external_chat_id)
            ticket = await self._get_typing_ticket(target.external_chat_id, context_token)
            if ticket:
                await self._api.send_typing(
                    ilink_user_id=target.external_chat_id,
                    typing_ticket=ticket,
                )
        except Exception:
            pass

    def _load_credentials(self) -> WeixinCredentials | None:
        if self._config.bot_token.strip() and self._config.account_id.strip():
            return WeixinCredentials(
                account_id=self._config.account_id.strip(),
                token=self._config.bot_token.strip(),
                base_url=self._config.base_url or DEFAULT_BASE_URL,
            )
        return self._state.load_credentials(
            account_id=self._config.account_id,
            credentials_path=self._config.credentials_path,
        )

    async def _poll_loop(self) -> None:
        if self._api is None or self._credentials is None:
            return
        account_id = self._credentials.account_id
        get_updates_buf = self._state.load_sync_buf(account_id)
        next_timeout_ms = self._config.poll_timeout_ms or DEFAULT_LONG_POLL_TIMEOUT_MS
        consecutive_failures = 0

        while not self._is_stopping():
            try:
                response = await self._api.get_updates(
                    get_updates_buf=get_updates_buf,
                    timeout_ms=next_timeout_ms,
                )
                next_timeout_ms = int(response.get("longpolling_timeout_ms") or next_timeout_ms)
                if self._is_api_error(response):
                    pause_ms = self._handle_api_error(response, consecutive_failures)
                    consecutive_failures = (
                        0
                        if pause_ms >= self._config.session_expired_pause_ms
                        else consecutive_failures + 1
                    )
                    await self._sleep_or_stop(pause_ms)
                    continue

                consecutive_failures = 0
                new_buf = response.get("get_updates_buf")
                if isinstance(new_buf, str) and new_buf:
                    get_updates_buf = new_buf
                    self._state.save_sync_buf(account_id, get_updates_buf)

                for message in response.get("msgs") or []:
                    if isinstance(message, dict):
                        await self._process_inbound_message(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive_failures += 1
                logger.exception(
                    "Weixin polling failed",
                    account_id=account_id,
                    consecutive_failures=consecutive_failures,
                )
                await self._sleep_or_stop(30_000 if consecutive_failures >= 3 else 2_000)
                if consecutive_failures >= 3:
                    consecutive_failures = 0

    async def _process_inbound_message(self, message: dict[str, Any]) -> None:
        if self._credentials is None:
            return
        if message.get("message_type") == MESSAGE_TYPE_BOT:
            return

        from_user_id = str(message.get("from_user_id") or "").strip()
        if not from_user_id or not self._is_user_allowed(from_user_id):
            return

        raw_items = message.get("item_list")
        text = body_from_item_list(raw_items if isinstance(raw_items, list) else None).strip()
        if not text:
            logger.info("Skipping unsupported Weixin message", from_user_id=from_user_id)
            return

        context_token = str(message.get("context_token") or "").strip()
        if context_token:
            self._context_tokens[from_user_id] = context_token
            self._state.save_context_tokens(self._credentials.account_id, self._context_tokens)

        if self._session_mapper is None:
            raise RuntimeError("Weixin channel session mapper is not bound")
        if self._message_dispatcher is None:
            raise RuntimeError("Weixin channel message dispatcher is not bound")

        mapping = await self._session_mapper.resolve_or_create(
            channel_type=self.channel_type,
            external_chat_id=from_user_id,
            external_user_id=from_user_id,
            is_group=False,
            display_name=f"Weixin: {from_user_id}",
        )

        await self.send_typing_indicator(
            ChannelTarget(channel_type=self.channel_type, external_chat_id=from_user_id)
        )

        metadata = {
            "channel_type": self.channel_type,
            "account_id": self._credentials.account_id,
            "external_chat_id": from_user_id,
            "external_user_id": from_user_id,
            "external_message_id": str(message.get("message_id") or message.get("seq") or ""),
            "context_token": context_token,
            "is_group": False,
        }
        outcome = await self._message_dispatcher.dispatch_user_message(
            source=self.channel_type,
            user_id=mapping.magi_user_id,
            session_id=mapping.magi_session_id,
            message=text,
            metadata=metadata,
        )
        if not outcome.success:
            logger.warning(
                "Weixin dispatch failed",
                error_code=outcome.error_code,
                error_message=outcome.error_message,
            )

    async def _get_typing_ticket(self, user_id: str, context_token: str | None) -> str:
        if self._api is None:
            return ""
        now_ms = int(time.time() * 1000)
        cached = self._typing_cache.get(user_id)
        if cached and now_ms < cached.next_refresh_at_ms:
            return cached.ticket
        response = await self._api.get_config(ilink_user_id=user_id, context_token=context_token)
        ticket = str(response.get("typing_ticket") or "") if response.get("ret") == 0 else ""
        self._typing_cache[user_id] = _CachedTypingTicket(
            ticket=ticket,
            next_refresh_at_ms=now_ms + 24 * 60 * 60 * 1000,
        )
        return ticket

    def _is_user_allowed(self, external_user_id: str) -> bool:
        if not self._config.allowed_user_ids:
            return True
        return external_user_id in self._config.allowed_user_ids

    @staticmethod
    def _is_api_error(response: dict[str, Any]) -> bool:
        ret = response.get("ret")
        errcode = response.get("errcode")
        return (ret is not None and ret != 0) or (errcode is not None and errcode != 0)

    def _handle_api_error(self, response: dict[str, Any], consecutive_failures: int) -> int:
        errcode = response.get("errcode")
        ret = response.get("ret")
        if errcode == SESSION_EXPIRED_ERRCODE or ret == SESSION_EXPIRED_ERRCODE:
            logger.error("Weixin session expired; polling paused", response=response)
            return self._config.session_expired_pause_ms
        logger.error(
            "Weixin getUpdates returned an error",
            response=response,
            consecutive_failures=consecutive_failures + 1,
        )
        return 30_000 if consecutive_failures >= 2 else 2_000

    def _is_stopping(self) -> bool:
        return bool(self._stop_event and self._stop_event.is_set())

    async def _sleep_or_stop(self, delay_ms: int) -> None:
        if self._stop_event is None:
            await asyncio.sleep(delay_ms / 1000)
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=max(delay_ms, 0) / 1000)
        except asyncio.TimeoutError:
            return
