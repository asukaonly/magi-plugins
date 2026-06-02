"""Weixin channel adapter for Magi."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from magi_plugin_sdk import get_logger
from magi_plugin_sdk.channels import (
    Channel,
    ChannelAttachmentStoreProtocol,
    ChannelMessageDispatcherProtocol,
    ChannelSessionMapperProtocol,
    ChannelTarget,
    OutboundContent,
)
from magi_plugin_sdk.delivery import DeliveryContent, DeliveryReceipt

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
from .media import collect_media_attachments
from .normalizers import body_from_item_list
from .state import WeixinCredentials, WeixinStateStore

logger = get_logger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


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
        self._attachment_store: ChannelAttachmentStoreProtocol | None = None
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

    def bind_attachment_store(self, attachment_store: ChannelAttachmentStoreProtocol) -> None:
        self._attachment_store = attachment_store

    async def start(self) -> None:
        self._state.update_channel_status(state="starting", running=False, configured=False, last_error="")
        credentials = self._load_credentials()
        if credentials is None:
            self._state.update_channel_status(
                state="unconfigured",
                running=False,
                configured=False,
                last_error="",
            )
            logger.info("Weixin channel is not configured; run QR login or set credentials_path/manual token")
            return
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
        self._state.update_channel_status(
            state="running",
            running=True,
            configured=True,
            account_id=credentials.account_id,
            base_url=self._api.base_url,
            last_start_at_ms=_now_ms(),
            last_error="",
        )
        logger.info("Weixin channel started account_id=%s", credentials.account_id)

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
        self._state.update_channel_status(state="stopped", running=False, last_stop_at_ms=_now_ms())
        logger.info("Weixin channel stopped")

    async def send_message(self, target: ChannelTarget, content: OutboundContent) -> None:
        """Legacy SDK path — fires and forgets. Phase G's ``deliver()`` is
        the modern caller and returns a receipt for retract/revise lookups."""
        await self._send_text(text=content.text or "", target=target)

    async def deliver(
        self, target: ChannelTarget, content: DeliveryContent,
    ) -> DeliveryReceipt:
        """Phase G typed delivery — returns a receipt carrying the
        Weixin-side ``client_id`` of the last sent chunk so the host's
        DeliveryReceiptsStore can correlate later operations.

        ``client_id`` is what Weixin uses as its per-message identity.
        For multi-chunk sends, we return the LAST chunk's id (consistent
        with how telegram and chat_sse handle their receipts).
        """
        client_ids = await self._send_text(
            text=content.text or "", target=target,
        )
        return DeliveryReceipt(
            channel_id="weixin",
            external_message_id=client_ids[-1] if client_ids else None,
            delivered_at_ms=_now_ms(),
            magi_session_id=target.magi_session_id,
        )

    async def _resolve_external_chat_id(self, target: ChannelTarget) -> str:
        """Return the WeChat-side ``to_user_id`` for this target.

        Phase G design: ``resolve_delivery_targets`` leaves
        ``target.external_chat_id`` blank for non-chat_sse channels and
        expects the channel to look it up itself via the session_mapper
        (the inbound flow already recorded a ``magi_session_id ↔
        external_chat_id`` mapping when the user first wrote to us on
        WeChat). Without this lookup, ``to_user_id=""`` reaches iLink's
        ``sendmessage`` endpoint and gets back ``ret: -2`` (param error).

        Callers may still pre-fill ``target.external_chat_id`` — we
        prefer it when present so the legacy ``send_message`` path
        (which does fill it) keeps working unchanged.
        """
        external = (target.external_chat_id or "").strip()
        if external:
            return external
        if self._session_mapper is None:
            return ""
        mapping = await self._session_mapper.lookup_by_session(
            target.magi_session_id
        )
        if mapping is None or mapping.channel_type != "weixin":
            return ""
        return str(mapping.external_chat_id or "").strip()

    async def _send_text(
        self, *, text: str, target: ChannelTarget,
    ) -> list[str]:
        """Core text-send used by both legacy send_message and Phase G deliver.

        Returns the list of Weixin client_ids assigned to each chunk
        (empty list when the input text is empty after stripping).
        """
        if self._api is None or self._credentials is None:
            raise RuntimeError("Weixin channel is not started")
        text = (text or "").strip()
        if not text:
            return []
        to_user_id = await self._resolve_external_chat_id(target)
        if not to_user_id:
            raise RuntimeError(
                f"Weixin deliver: no external_chat_id resolvable for "
                f"magi_session_id={target.magi_session_id!r} "
                f"(session_mapper has no weixin mapping for this session)"
            )
        context_token = self._context_tokens.get(to_user_id)
        client_ids: list[str] = []
        try:
            for chunk in self._split_text(text):
                client_ids.append(
                    await self._api.send_text_message(
                        to_user_id=to_user_id,
                        text=chunk,
                        context_token=context_token,
                        timeout_ms=self._config.request_timeout_ms,
                    )
                )
        except Exception as exc:
            self._state.update_channel_status(
                state="degraded",
                running=True,
                last_error=str(exc),
                last_error_at_ms=_now_ms(),
            )
            raise
        self._state.update_channel_status(
            state="running",
            running=True,
            last_outbound_at_ms=_now_ms(),
            last_outbound_chat_id=to_user_id,
            last_outbound_client_id=client_ids[-1] if client_ids else "",
            last_outbound_part_count=len(client_ids),
            last_error="",
        )
        logger.info(
            "Weixin outbound sent to_user_id=%s client_id=%s chars=%s",
            to_user_id,
            client_ids[-1] if client_ids else "",
            len(text),
        )
        return client_ids

    async def send_typing_indicator(self, target: ChannelTarget) -> None:
        if not self._config.enable_typing_indicator or self._api is None:
            return
        try:
            to_user_id = await self._resolve_external_chat_id(target)
            if not to_user_id:
                return
            context_token = self._context_tokens.get(to_user_id)
            ticket = await self._get_typing_ticket(to_user_id, context_token)
            if ticket:
                await self._api.send_typing(
                    ilink_user_id=to_user_id,
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
        processed_message_ids = self._state.load_processed_message_ids(account_id)
        next_timeout_ms = self._config.poll_timeout_ms or DEFAULT_LONG_POLL_TIMEOUT_MS
        consecutive_failures = 0

        while not self._is_stopping():
            try:
                response = await self._api.get_updates(
                    get_updates_buf=get_updates_buf,
                    timeout_ms=next_timeout_ms,
                )
                self._state.update_channel_status(
                    state="running",
                    running=True,
                    configured=True,
                    account_id=account_id,
                    last_poll_at_ms=_now_ms(),
                    last_error="",
                )
                next_timeout_ms = int(response.get("longpolling_timeout_ms") or next_timeout_ms)
                if self._is_api_error(response):
                    pause_ms = self._handle_api_error(response, consecutive_failures, account_id)
                    consecutive_failures = (
                        0
                        if pause_ms >= self._config.session_expired_pause_ms
                        else consecutive_failures + 1
                    )
                    await self._sleep_or_stop(pause_ms)
                    continue

                consecutive_failures = 0
                messages = [message for message in (response.get("msgs") or []) if isinstance(message, dict)]
                all_processed = True
                processed_changed = False
                for message in response.get("msgs") or []:
                    if isinstance(message, dict):
                        message_key = self._message_key(message)
                        if message_key and message_key in processed_message_ids:
                            continue
                        processed = await self._process_inbound_message(message)
                        if not processed:
                            all_processed = False
                            break
                        if message_key:
                            processed_message_ids.add(message_key)
                            processed_changed = True

                if processed_changed:
                    self._state.save_processed_message_ids(account_id, processed_message_ids)

                new_buf = response.get("get_updates_buf")
                if isinstance(new_buf, str) and new_buf and all_processed:
                    get_updates_buf = new_buf
                    self._state.save_sync_buf(account_id, get_updates_buf)
                elif isinstance(new_buf, str) and new_buf and messages:
                    logger.warning("Weixin getUpdates cursor not advanced because a message was not processed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                self._state.update_channel_status(
                    state="degraded",
                    running=True,
                    last_error=str(exc),
                    last_error_at_ms=_now_ms(),
                )
                logger.exception(
                    "Weixin polling failed account_id=%s consecutive_failures=%s",
                    account_id,
                    consecutive_failures,
                )
                await self._sleep_or_stop(30_000 if consecutive_failures >= 3 else 2_000)
                if consecutive_failures >= 3:
                    consecutive_failures = 0

    async def _process_inbound_message(self, message: dict[str, Any]) -> bool:
        if self._credentials is None:
            return False
        if message.get("message_type") == MESSAGE_TYPE_BOT:
            return True

        from_user_id = str(message.get("from_user_id") or "").strip()
        if not from_user_id or not self._is_user_allowed(from_user_id):
            return True

        raw_items = message.get("item_list")
        text = body_from_item_list(raw_items if isinstance(raw_items, list) else None).strip()
        message_key = self._message_key(message)
        client_turn_id = self._client_turn_id(message_key)

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

        attachments: list[dict[str, Any]] = []
        media_errors: list[str] = []
        if self._attachment_store is not None and isinstance(raw_items, list):
            media_result = await collect_media_attachments(
                raw_items,
                attachment_store=self._attachment_store,
                cdn_base_url=self._config.cdn_base_url,
                session_id=mapping.magi_session_id,
                turn_id=client_turn_id,
                message_key=message_key,
            )
            attachments = media_result.attachments
            media_errors = media_result.errors

        if not text and not attachments:
            logger.info("Skipping unsupported Weixin message from_user_id=%s", from_user_id)
            return True

        reply_to_external_id = self._reply_to_external_id(raw_items if isinstance(raw_items, list) else None)
        reply_to_message_id = (
            self._state.lookup_message_id_mapping(self._credentials.account_id, reply_to_external_id)
            if reply_to_external_id
            else None
        )

        metadata = {
            "channel_type": self.channel_type,
            "account_id": self._credentials.account_id,
            "external_chat_id": from_user_id,
            "external_user_id": from_user_id,
            "external_message_id": self._external_message_id(message),
            "reply_to_external_id": reply_to_external_id or "",
            "context_token": context_token,
            "is_group": False,
            "attachment_count": len(attachments),
            "media_errors": media_errors,
        }
        outcome = await self._message_dispatcher.dispatch_user_message(
            source=self.channel_type,
            user_id=mapping.magi_user_id,
            session_id=mapping.magi_session_id,
            message=text,
            attachments=attachments,
            reply_to_message_id=reply_to_message_id,
            client_turn_id=client_turn_id,
            metadata=metadata,
        )
        if not outcome.success:
            logger.warning(
                "Weixin dispatch failed error_code=%s error_message=%s",
                outcome.error_code,
                outcome.error_message,
            )
            self._state.update_channel_status(
                state="degraded",
                running=True,
                last_error=outcome.error_message or outcome.error_code or "Weixin dispatch failed",
                last_error_at_ms=_now_ms(),
            )
            return False
        magi_message_id = str(getattr(outcome, "message_id", "") or "").strip()
        external_message_id = self._external_message_id(message)
        if magi_message_id and external_message_id:
            self._state.save_message_id_mapping(self._credentials.account_id, external_message_id, magi_message_id)
        self._state.update_channel_status(
            state="running",
            running=True,
            last_inbound_at_ms=_now_ms(),
            last_inbound_chat_id=from_user_id,
            last_error="",
        )
        return True

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

    def _handle_api_error(self, response: dict[str, Any], consecutive_failures: int, account_id: str) -> int:
        errcode = response.get("errcode")
        ret = response.get("ret")
        if errcode == SESSION_EXPIRED_ERRCODE or ret == SESSION_EXPIRED_ERRCODE:
            self._state.update_channel_status(
                state="failed",
                running=False,
                account_id=account_id,
                last_error="Weixin session expired. Run QR login again.",
                last_error_at_ms=_now_ms(),
            )
            logger.error("Weixin session expired; polling paused response=%s", response)
            return self._config.session_expired_pause_ms
        self._state.update_channel_status(
            state="degraded",
            running=True,
            account_id=account_id,
            last_error=str(response),
            last_error_at_ms=_now_ms(),
        )
        logger.error(
            "Weixin getUpdates returned an error response=%s consecutive_failures=%s",
            response,
            consecutive_failures + 1,
        )
        return 30_000 if consecutive_failures >= 2 else 2_000

    @staticmethod
    def _message_key(message: dict[str, Any]) -> str:
        for key in ("message_id", "seq", "client_id"):
            value = str(message.get(key) or "").strip()
            if value:
                return value
        raw = json.dumps(message, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _split_text(self, text: str) -> list[str]:
        limit = max(1, int(self._config.max_message_length or 4000))
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n", 0, limit + 1)
            if split_at < max(1, limit // 2):
                split_at = remaining.rfind(" ", 0, limit + 1)
            if split_at < max(1, limit // 2):
                split_at = limit
            chunks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()
        return [chunk for chunk in chunks if chunk]

    @staticmethod
    def _external_message_id(message: dict[str, Any]) -> str:
        for key in ("message_id", "seq", "client_id"):
            value = str(message.get(key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _client_turn_id(message_key: str) -> str:
        digest = hashlib.sha256(message_key.encode("utf-8")).hexdigest()[:24]
        return f"weixin_{digest}"

    @staticmethod
    def _reply_to_external_id(item_list: list[dict[str, Any]] | None) -> str:
        for item in item_list or []:
            ref = item.get("ref_msg")
            if not isinstance(ref, dict):
                continue
            candidates: list[dict[str, Any]] = [ref]
            message_item = ref.get("message_item")
            if isinstance(message_item, dict):
                candidates.append(message_item)
            for candidate in candidates:
                for key in ("message_id", "msg_id", "seq", "client_id"):
                    value = str(candidate.get(key) or "").strip()
                    if value:
                        return value
        return ""

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
