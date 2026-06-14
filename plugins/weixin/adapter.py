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
from magi_plugin_sdk import ControlRequest
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

    # === Phase H+2: opt into control-plane fanout ===
    # WeChat has no inline-button primitive, so deliver_control_request
    # renders the prompt as a text message with explicit ``/approve
    # <short_id>`` / ``/deny <short_id>`` instructions. The user's
    # text reply flows through the normal inbound path; CF-6's
    # slash-command parser resolves the broker.
    supports_control_requests = True

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
        self._control_port: Any = None
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

    def bind_control_port(self, control_port: Any) -> None:
        self._control_port = control_port

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

        Phase A media-outbound: when ``content.attachments`` is non-empty,
        each image attachment is AES-128-ECB-encrypted client-side,
        uploaded to the iLink CDN, and sent as a follow-up image message
        AFTER the text body — same ordering as Telegram. Non-image
        attachments are currently logged-and-skipped (iLink does support
        voice/file/video item types via the same 3-step pipeline, but
        each needs its own metadata which isn't wired yet). The receipt
        tracks the LAST sent ``client_id`` so a subsequent retract
        operates on the most recent message.
        """
        # Phase H+2 diagnostics: log every deliver entry so we can
        # tell whether attachments are reaching the channel at all
        # vs being lost upstream (e.g. orchestrator dropping them
        # before fanout). Logged at INFO so it shows in default
        # backend logs.
        attachments_in = list(content.attachments or ())
        attachment_kinds = [str(a.get("kind") or "?") for a in attachments_in]
        logger.info(
            "Weixin.deliver entry session=%s text_len=%d attachments=%d kinds=%s",
            target.magi_session_id,
            len(content.text or ""),
            len(attachments_in),
            attachment_kinds,
        )
        client_ids = await self._send_text(
            text=content.text or "", target=target,
        )
        if attachments_in:
            attachment_ids = await self._send_attachments(
                attachments=attachments_in, target=target,
            )
            client_ids.extend(attachment_ids)
            logger.info(
                "Weixin.deliver attachments sent session=%s sent=%d/%d",
                target.magi_session_id,
                len(attachment_ids), len(attachments_in),
            )
        return DeliveryReceipt(
            channel_id="weixin",
            external_message_id=client_ids[-1] if client_ids else None,
            delivered_at_ms=_now_ms(),
            magi_session_id=target.magi_session_id,
        )

    async def deliver_control_request(
        self,
        target: ChannelTarget,
        request: ControlRequest,
    ) -> None:
        """Phase H+2 — render a permission prompt as a text message
        with explicit slash-command instructions.

        Unlike Telegram (inline buttons), WeChat has no out-of-band
        UX for "tap to act" — the only reliable response primitive
        is the user typing a reply. So we send a clear text block
        telling the user exactly what to type. The host's CF-6
        slash-command parser resolves the broker when the reply
        arrives via _on_inbound_message.

        Truncates preview defensively to keep the message under the
        iLink text-length cap (the host already truncated to 200
        chars; we leave room for the wrapping text).
        """
        preview = request.preview or "(no preview)"
        # Compact, scannable format. Users on phones won't read long
        # blocks of text. The slash-command parser also accepts plain
        # 同意 / 拒绝 (and ok / yes / no) when there's only one
        # pending request, so we surface the natural-language path as
        # primary and the explicit ID path as fallback.
        text = (
            f"⚠️ Magi 想运行工具:{request.tool_name}\n"
            f"\n"
            f"{preview}\n"
            f"\n"
            f"回复 同意 或 拒绝\n"
            f"(同时有多个时请加 ID: 同意 {request.short_id})"
        )
        try:
            await self._send_text(text=text, target=target)
        except Exception:
            # _send_text already logs; swallow so the fanout in the
            # host (DeliveryRouter.fanout_control_request) doesn't see
            # an exception that aborts other channels.
            logger.exception(
                "Weixin deliver_control_request failed "
                "session=%s short_id=%s",
                target.magi_session_id, request.short_id,
            )

    async def _send_attachments(
        self,
        *,
        attachments: list[dict[str, Any]],
        target: ChannelTarget,
    ) -> list[str]:
        """Send each attachment via the iLink media pipeline.

        Currently handles ``kind="image"`` only — non-image kinds are
        logged and skipped rather than dropped silently. Best-effort per
        attachment: one failure doesn't abort siblings.
        """
        if self._api is None or self._credentials is None:
            raise RuntimeError("Weixin channel is not started")
        to_user_id = await self._resolve_external_chat_id(target)
        if not to_user_id:
            raise RuntimeError(
                "Weixin deliver(attachments): no external_chat_id resolvable for "
                f"magi_session_id={target.magi_session_id!r}"
            )
        context_token = self._context_tokens.get(to_user_id)
        client_ids: list[str] = []
        for attachment in attachments:
            kind = str(attachment.get("kind") or "").strip().lower()
            if kind != "image":
                logger.warning(
                    "Weixin attachment skipped (only image is wired today) "
                    "kind=%s attachment_id=%s original_name=%s",
                    kind, attachment.get("attachment_id"),
                    attachment.get("original_name"),
                )
                continue
            try:
                client_id = await self._send_image_attachment(
                    attachment=attachment,
                    to_user_id=to_user_id,
                    context_token=context_token,
                )
                client_ids.append(client_id)
            except Exception:
                logger.exception(
                    "Weixin attachment send failed attachment_id=%s "
                    "storage_path=%s",
                    attachment.get("attachment_id"),
                    attachment.get("storage_path"),
                )
                # Keep going — best-effort across siblings.
        return client_ids

    async def _send_image_attachment(
        self,
        *,
        attachment: dict[str, Any],
        to_user_id: str,
        context_token: str | None,
    ) -> str:
        """Encrypt → upload → send for one image. Returns iLink client_id.

        The 3-step iLink flow:
          1. getuploadurl → CDN credentials + PUT URLs (main + thumb).
          2. PUT AES-128-ECB-encrypted ciphertext to each URL.
          3. sendmessage with item_list[type=2] referencing CDNMedia.
        """
        from . import media_upload as _mu
        from .api import UPLOAD_MEDIA_TYPE_IMAGE, WeixinApiError

        attachment_id = str(attachment.get("attachment_id") or "")
        logger.info(
            "Weixin _send_image_attachment START attachment_id=%s to_user_id=%s "
            "attachment_keys=%s",
            attachment_id, to_user_id, sorted(attachment.keys()),
        )

        storage_path = str(attachment.get("storage_path") or "").strip()
        if not storage_path:
            # Defensive fallback: derive absolute from storage_rel_path
            # which the chat layer always sets. Some upstream paths
            # only carry the relative form (chat_messages.payload_json
            # is the canonical example), so use it when storage_path
            # is missing. Both point to the same file.
            rel_path = str(attachment.get("storage_rel_path") or "").strip()
            if rel_path:
                from pathlib import Path
                from magi_plugin_sdk import PluginRuntimePaths  # noqa: F401  (typing only)
                # Best-effort: assume the standard runtime layout
                # (~/.magi/data/...). Real-world this lives in
                # magi.utils.runtime.get_runtime_paths but plugins
                # shouldn't pull host internals — fall back to
                # XDG-style default.
                import os
                base = Path(os.environ.get("MAGI_DATA_DIR") or
                            (Path.home() / ".magi"))
                storage_path = str(base / rel_path)
                logger.info(
                    "Weixin storage_path derived from rel: %s",
                    storage_path,
                )
            else:
                logger.error(
                    "Weixin attachment missing both storage_path and "
                    "storage_rel_path attachment_id=%s keys=%s",
                    attachment_id, sorted(attachment.keys()),
                )
                raise ValueError(
                    "attachment is missing storage_path; "
                    "host-side attachment_ingestion should always set it"
                )
        try:
            with open(storage_path, "rb") as fh:
                plaintext = fh.read()
        except FileNotFoundError:
            logger.error(
                "Weixin file not found attachment_id=%s storage_path=%s",
                attachment_id, storage_path,
            )
            raise
        logger.info(
            "Weixin file read OK attachment_id=%s size=%d",
            attachment_id, len(plaintext),
        )

        # --- AES-128 key + main file encryption ---
        key = _mu.generate_aes_key()
        ciphertext = _mu.aes_128_ecb_encrypt(plaintext, key)
        raw_md5 = _mu.md5_hex(plaintext)
        # iLink protocol uses TWO encodings of the same AES key:
        #   - aes_key_hex (32 lowercase hex chars) → in getuploadurl
        #     body's "aeskey" field. Verified against
        #     openclaw-weixin src/cdn/upload.ts:81
        #     ``aeskey: aeskey.toString("hex")``.
        #   - aes_key_msg_b64 (base64 of the hex string's UTF-8
        #     bytes) → in sendmessage's media.aes_key field.
        #     Verified against openclaw src/messaging/send.ts:
        #     ``aes_key: Buffer.from(uploaded.aeskey).toString("base64")``
        #     where uploaded.aeskey is the hex string. Note this
        #     is base64-of-hex-string, NOT base64-of-raw-key-bytes.
        aes_key_hex = key.hex()
        aes_key_msg_b64 = _mu.b64encode(aes_key_hex.encode("utf-8"))

        # --- thumbnail: openclaw reference (src/cdn/upload.ts:80)
        # unconditionally sends ``no_need_thumb: true`` — the server
        # generates previews from the uploaded ciphertext. Our prior
        # client-side thumbnail pipeline was a guess from outdated
        # docs; dropping it removes a moving part that doesn't add
        # value. The thumb-related fields below are still computed
        # for compat with get_upload_url's signature but no actual
        # thumb is uploaded. ---
        no_need_thumb = True
        thumb_bytes = b""
        thumb_cipher = b""
        thumb_raw_md5 = ""
        thumb_w = thumb_h = 0

        # filekey: per-upload random hex (matches openclaw
        # src/cdn/upload.ts:66 ``crypto.randomBytes(16).toString("hex")``).
        # Previously this plugin reused ``attachment_id`` as the
        # filekey, which made retries of the same attachment collide
        # on the iLink server's internal dedup table. A fresh random
        # filekey per upload eliminates that whole class of issue.
        import secrets as _secrets
        filekey = _secrets.token_hex(16)

        logger.info(
            "Weixin requesting upload URL attachment_id=%s filekey=%s "
            "raw_size=%d cipher_size=%d thumb_raw_size=%d no_need_thumb=%s",
            attachment_id, filekey, len(plaintext), len(ciphertext),
            len(thumb_bytes), no_need_thumb,
        )
        upload_resp = await self._api.get_upload_url(
            filekey=filekey,
            media_type=UPLOAD_MEDIA_TYPE_IMAGE,
            to_user_id=to_user_id,
            raw_size=len(plaintext),
            raw_md5=raw_md5,
            cipher_size=len(ciphertext),
            thumb_raw_size=len(thumb_bytes),
            thumb_raw_md5=thumb_raw_md5,
            thumb_cipher_size=len(thumb_cipher),
            aes_key_hex=aes_key_hex,
            no_need_thumb=no_need_thumb,
            timeout_ms=self._config.request_timeout_ms,
        )
        # Verbose dump — first 300 chars of every field so we can
        # see what iLink actually returned. Some deployments return
        # extra fields (CDN host hints, expiry stamps, etc.) we may
        # need to use.
        _resp_preview = {
            k: (str(v)[:300] if v is not None else None)
            for k, v in (upload_resp or {}).items()
        }
        logger.info(
            "Weixin upload URL response attachment_id=%s keys=%s preview=%s",
            attachment_id, sorted((upload_resp or {}).keys()), _resp_preview,
        )

        upload_param_val = str(upload_resp.get("upload_param") or "")
        upload_full_url_val = str(upload_resp.get("upload_full_url") or "")

        # CDN upload — new signature handles both response shapes:
        #   - has upload_full_url → use it directly
        #   - has only upload_param → build URL from cdn_base + param
        # Returns the download_encrypted_query_param (x-encrypted-param
        # response header) — that's the token to embed in sendmessage,
        # NOT the upload_param we just used to upload.
        logger.info(
            "Weixin uploading main CDN attachment_id=%s "
            "have_full_url=%s have_param=%s cipher_size=%d",
            attachment_id,
            bool(upload_full_url_val), bool(upload_param_val),
            len(ciphertext),
        )
        image_download_param = await self._api.upload_to_cdn(
            upload_full_url=upload_full_url_val or None,
            upload_param=upload_param_val or None,
            filekey=filekey,
            cdn_base_url=self._config.cdn_base_url,
            encrypted_bytes=ciphertext,
            timeout_ms=self._config.request_timeout_ms,
        )
        logger.info(
            "Weixin main CDN upload OK attachment_id=%s "
            "download_param_len=%d",
            attachment_id, len(image_download_param),
        )
        # Thumb upload removed — openclaw reference uses
        # ``no_need_thumb: true`` unconditionally (server generates
        # the preview). See above for the rationale.

        # send_image_message's ``image_param`` is semantically
        # the CDN DOWNLOAD token (image_download_param), NOT the
        # upload_param. Previously misnamed/misrouted; verified
        # against openclaw src/messaging/send.ts which uses
        # ``uploaded.downloadEncryptedQueryParam``.
        client_id = await self._api.send_image_message(
            to_user_id=to_user_id,
            image_param=image_download_param,
            image_aes_key_b64=aes_key_msg_b64,
            image_size=len(ciphertext),
            thumb_param=None,
            thumb_aes_key_b64=None,
            thumb_width=None,
            thumb_height=None,
            thumb_size=None,
            context_token=context_token,
            timeout_ms=self._config.request_timeout_ms,
        )
        self._state.update_channel_status(
            state="running",
            running=True,
            last_outbound_at_ms=_now_ms(),
            last_outbound_chat_id=to_user_id,
            last_outbound_client_id=client_id,
            last_error="",
        )
        logger.info(
            "Weixin outbound image sent to_user_id=%s client_id=%s "
            "attachment_id=%s raw_bytes=%s cipher_bytes=%s",
            to_user_id, client_id,
            attachment.get("attachment_id"),
            len(plaintext), len(ciphertext),
        )
        return client_id

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

        # Control commands (/new, /reset, /approve, /help) — handled by the host's
        # unified control port and surfaced to the sender; no LLM turn. Sent
        # straight to from_user_id (a /新会话 reset has just deleted the mapping,
        # so _send_text's session-mapping resolution would otherwise fail).
        if self._control_port is not None and text and text.strip():
            result = await self._control_port.handle_command(
                message=text,
                session_id=mapping.magi_session_id,
                channel_type=self.channel_type,
                external_chat_id=from_user_id,
                external_user_id=from_user_id,
            )
            if result is not None:
                if result.ack and self._api is not None:
                    try:
                        await self._api.send_text_message(
                            to_user_id=from_user_id,
                            text=result.ack,
                            context_token=context_token or None,
                            timeout_ms=self._config.request_timeout_ms,
                        )
                    except Exception:
                        logger.warning("Weixin control-command ack send failed", exc_info=True)
                return True

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
