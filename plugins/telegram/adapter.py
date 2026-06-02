"""Telegram channel adapter — python-telegram-bot based implementation."""

from __future__ import annotations

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
from magi_plugin_sdk.delivery import DeliveryContent, DeliveryReceipt

from .formatter import telegram_format

logger = get_logger(__name__)


@dataclass
class TelegramChannelConfig:
    """Telegram-specific channel configuration."""

    bot_token: str = ""
    mode: str = "polling"  # "polling" | "webhook"
    webhook_url: str = ""
    webhook_secret: str = ""
    proxy: str = ""
    allowed_user_ids: list[str] = field(default_factory=list)
    group_trigger_keyword: str = ""
    magi_user_id: str = "default"
    max_message_length: int = 4096


class TelegramChannel(Channel):
    """Bidirectional Telegram bot channel."""

    def __init__(
        self,
        *,
        config: TelegramChannelConfig,
        session_mapper: ChannelSessionMapperProtocol | None = None,
        message_dispatcher: ChannelMessageDispatcherProtocol | None = None,
    ) -> None:
        self._config = config
        self._session_mapper = session_mapper
        self._message_dispatcher = message_dispatcher
        self._application: Any = None
        self._bot_username: str = ""
        self._bot_id: int = 0

    def bind_session_mapper(self, session_mapper: ChannelSessionMapperProtocol) -> None:
        self._session_mapper = session_mapper

    def bind_message_dispatcher(self, dispatcher: ChannelMessageDispatcherProtocol) -> None:
        self._message_dispatcher = dispatcher

    @property
    def channel_type(self) -> str:
        return "telegram"

    async def start(self) -> None:
        try:
            from telegram.ext import (
                Application,
                CommandHandler,
                MessageHandler,
                filters,
            )
        except ImportError:
            raise RuntimeError(
                "python-telegram-bot is not installed. "
                "Install with: pip install 'python-telegram-bot>=22.0'"
            )

        if not self._config.bot_token:
            raise ValueError("Telegram bot token is required")

        builder = Application.builder().token(self._config.bot_token)
        if self._config.proxy:
            builder = builder.proxy(self._config.proxy).get_updates_proxy(self._config.proxy)

        self._application = builder.build()

        self._application.add_handler(CommandHandler("start", self._on_start_command))
        self._application.add_handler(CommandHandler("reset", self._on_reset_command))
        self._application.add_handler(CommandHandler("ask", self._on_ask_command))
        self._application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )

        await self._application.initialize()
        bot_info = await self._application.bot.get_me()
        self._bot_username = bot_info.username or ""
        self._bot_id = bot_info.id
        logger.info(
            "Telegram bot initialized",
            username=self._bot_username,
            bot_id=self._bot_id,
        )

        if self._config.mode == "webhook" and self._config.webhook_url:
            await self._application.bot.set_webhook(
                url=self._config.webhook_url,
                secret_token=self._config.webhook_secret or None,
            )
            await self._application.start()
            logger.info("Telegram channel started in webhook mode")
        else:
            await self._application.start()
            await self._application.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram channel started in polling mode")

    async def stop(self) -> None:
        if self._application is None:
            return
        try:
            if self._application.updater and self._application.updater.running:
                await self._application.updater.stop()
            await self._application.stop()
            await self._application.shutdown()
        except Exception:
            logger.exception("Error stopping Telegram channel")
        self._application = None

    async def send_message(self, target: ChannelTarget, content: OutboundContent) -> None:
        if self._application is None:
            return
        text = telegram_format(content.text, max_length=self._config.max_message_length)
        if not text.strip():
            return
        kwargs: dict[str, Any] = {
            "chat_id": int(target.external_chat_id),
            "text": text,
        }
        # Try MarkdownV2 first; fall back to plain text on parse error.
        try:
            await self._application.bot.send_message(parse_mode="MarkdownV2", **kwargs)
        except Exception:
            logger.debug("MarkdownV2 send failed, retrying as plain text")
            kwargs["text"] = content.text[: self._config.max_message_length]
            try:
                await self._application.bot.send_message(**kwargs)
            except Exception:
                logger.exception(
                    "Failed to send message to Telegram",
                    chat_id=target.external_chat_id,
                )

    # === Phase G capability flags ===
    # supports_streaming = False (SDK default): Telegram has no native streaming
    # UX. The earlier deliver_chunk implementation (buffer-until-final then
    # self.deliver) caused double-delivery once the coordinator's fanout_deliver
    # path also fired with the full text. DeliveryRouter.fanout_chunk now skips
    # non-streaming channels; Telegram receives only the assembled deliver().
    supports_revision = True
    supports_attachments = True

    # === Attachment routing ===
    # Map ``DeliveryContent.attachments[i]["kind"]`` (from the host's
    # attachment_ingestion classifier, which sets one of "image" /
    # "video" / "audio" / "document" / "file") onto the Telegram bot
    # API method that best represents it. Anything we don't recognise
    # falls through to send_document — the safe catch-all.
    _ATTACHMENT_KIND_METHOD: dict[str, str] = {
        "image": "send_photo",
        "video": "send_video",
        "audio": "send_audio",
        "voice": "send_voice",
        "animation": "send_animation",
        "document": "send_document",
    }
    # Telegram's caption character cap — applies uniformly to photo /
    # video / document / audio / animation. Text longer than this gets
    # sent as a separate text message BEFORE the media so the user sees
    # the narrative in order.
    _CAPTION_MAX_CHARS = 1024

    @staticmethod
    def _attachment_method(kind: str | None, mime_type: str | None) -> str:
        kind = (kind or "").strip().lower()
        mime = (mime_type or "").strip().lower()
        # Animated GIFs and short MP4s are best rendered as animations
        # (inline auto-play in Telegram) even when the ingestor tagged
        # them generically. Keeps the UX closer to what the user sees
        # in the chat UI.
        if mime in ("image/gif",) or (
            kind == "video" and mime in ("video/mp4",)
        ):
            return "send_animation"
        return TelegramChannel._ATTACHMENT_KIND_METHOD.get(kind, "send_document")

    async def _send_one_attachment(
        self,
        *,
        chat_id: int,
        attachment: dict[str, Any],
        caption: str | None,
    ) -> Any:
        """Pick the right send_* method, open the file, and call it.

        Returns the resulting Message (so the caller can record its
        message_id on the receipt). The caller is responsible for
        truncating the caption — we forward it untouched.
        """
        storage_path = str(attachment.get("storage_path") or "").strip()
        if not storage_path:
            raise ValueError(
                "attachment is missing storage_path; "
                "host-side attachment_ingestion should always set it"
            )
        method_name = self._attachment_method(
            attachment.get("kind"), attachment.get("mime_type"),
        )
        method = getattr(self._application.bot, method_name)
        # The keyword name differs per method (photo / video / document / …).
        media_kwarg = method_name.replace("send_", "")
        # File handles are opened with a context manager so PTB can read
        # them and release the fd; PTB accepts file-like objects directly.
        with open(storage_path, "rb") as fh:
            kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                media_kwarg: fh,
            }
            if caption:
                kwargs["caption"] = caption
            return await method(**kwargs)

    async def deliver(self, target: ChannelTarget, content: "DeliveryContent") -> "DeliveryReceipt":
        """Phase G delivery returning a DeliveryReceipt with the native
        Telegram message_id so the host can later edit / delete the message.

        Phase A media-outbound: when ``content.attachments`` is non-empty,
        attachments are sent in addition to (and AFTER) the text. The
        first attachment carries the text as its caption when the text is
        short enough; otherwise the text goes out as its own message first
        so nothing gets truncated. The receipt's ``external_message_id``
        tracks the LAST sent message (consistent with how multi-chunk
        text was already handled and how Weixin returns the last
        client_id), so retract operates on the most recent surface.
        """
        from magi_plugin_sdk.delivery import DeliveryContent, DeliveryReceipt  # noqa: F401
        import time

        chat_id = int(target.external_chat_id)
        text = telegram_format(content.text, max_length=self._config.max_message_length)
        if not text.strip() and content.text:
            text = content.text[: self._config.max_message_length]
        attachments = list(content.attachments or ())

        last_message = None

        # Decide where the text goes:
        # - No attachments: send text as before (legacy text-only path).
        # - 1+ attachments AND text fits in a single caption: bundle the
        #   text on the first attachment so we save a round-trip and
        #   render as one "card" in Telegram.
        # - 1+ attachments AND text doesn't fit: send text as a standalone
        #   message first, then each attachment without a caption.
        if not attachments:
            # Legacy text-only path — unchanged behavior.
            kwargs: dict[str, Any] = {"chat_id": chat_id, "text": text}
            try:
                last_message = await self._application.bot.send_message(
                    parse_mode="MarkdownV2", **kwargs
                )
            except Exception:
                logger.debug("MarkdownV2 deliver failed, retrying as plain text")
                kwargs["text"] = content.text[: self._config.max_message_length]
                try:
                    last_message = await self._application.bot.send_message(**kwargs)
                except Exception:
                    logger.exception(
                        "Failed to deliver message to Telegram",
                        chat_id=target.external_chat_id,
                    )
        else:
            # Decide whether the caption can carry the full text.
            inline_caption: str | None = None
            preamble_text = ""
            if text and len(text) <= self._CAPTION_MAX_CHARS:
                inline_caption = text
            elif text:
                preamble_text = text

            if preamble_text:
                try:
                    last_message = await self._application.bot.send_message(
                        chat_id=chat_id, text=preamble_text, parse_mode="MarkdownV2",
                    )
                except Exception:
                    logger.debug("MarkdownV2 preamble failed, retrying as plain text")
                    try:
                        last_message = await self._application.bot.send_message(
                            chat_id=chat_id,
                            text=content.text[: self._config.max_message_length],
                        )
                    except Exception:
                        logger.exception(
                            "Failed to deliver preamble text to Telegram",
                            chat_id=target.external_chat_id,
                        )

            for i, attachment in enumerate(attachments):
                # Caption rides on the FIRST attachment only (and only
                # when we didn't already send a preamble).
                caption = inline_caption if i == 0 and not preamble_text else None
                try:
                    last_message = await self._send_one_attachment(
                        chat_id=chat_id,
                        attachment=attachment,
                        caption=caption,
                    )
                except Exception:
                    logger.exception(
                        "Failed to deliver attachment to Telegram "
                        "chat_id=%s attachment_id=%s kind=%s storage_path=%s",
                        target.external_chat_id,
                        attachment.get("attachment_id"),
                        attachment.get("kind"),
                        attachment.get("storage_path"),
                    )
                    # Keep going — best-effort: one bad attachment
                    # shouldn't suppress siblings.

        # external_message_id is stored as "<chat_id>:<message_id>" so that
        # retract/revise can recover the chat_id without relying on channel_id.
        # channel_id is set to self.channel_type ("telegram") so ChannelRegistry
        # lookups (which index by channel_type) succeed.
        ext_msg_id = f"{chat_id}:{last_message.message_id}" if last_message is not None else None
        return DeliveryReceipt(
            channel_id=self.channel_type,
            external_message_id=ext_msg_id,
            delivered_at_ms=int(time.time() * 1000),
        )

    async def revise(self, receipt: "DeliveryReceipt", new_content: "DeliveryContent") -> "DeliveryReceipt":
        """Edit a previously-delivered Telegram message."""
        from magi_plugin_sdk.delivery import DeliveryContent, DeliveryReceipt  # noqa: F401
        import time

        if receipt.external_message_id is None:
            # Nothing was delivered (e.g. send failed), nothing to revise.
            return DeliveryReceipt(
                channel_id=receipt.channel_id,
                external_message_id=None,
                delivered_at_ms=int(time.time() * 1000),
            )

        chat_id = self._chat_id_from_receipt(receipt)
        text = telegram_format(new_content.text, max_length=self._config.max_message_length)
        if not text.strip():
            text = new_content.text[: self._config.max_message_length]

        message_id = self._message_id_from_receipt(receipt)
        try:
            await self._application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="MarkdownV2",
            )
        except Exception:
            logger.debug("MarkdownV2 revise failed, retrying as plain text")
            try:
                await self._application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=new_content.text[: self._config.max_message_length],
                )
            except Exception:
                logger.exception(
                    "Failed to revise Telegram message",
                    chat_id=chat_id,
                    message_id=receipt.external_message_id,
                )

        return DeliveryReceipt(
            channel_id=receipt.channel_id,
            external_message_id=receipt.external_message_id,
            delivered_at_ms=int(time.time() * 1000),
        )

    async def retract(self, receipt: "DeliveryReceipt") -> None:
        """Delete a previously-delivered Telegram message."""
        from magi_plugin_sdk.delivery import DeliveryReceipt  # noqa: F401

        if receipt.external_message_id is None:
            # Nothing was delivered (e.g. send failed), nothing to delete.
            return

        chat_id = self._chat_id_from_receipt(receipt)
        message_id = self._message_id_from_receipt(receipt)
        try:
            await self._application.bot.delete_message(
                chat_id=chat_id,
                message_id=message_id,
            )
        except Exception:
            logger.exception(
                "Failed to retract Telegram message",
                chat_id=chat_id,
                message_id=receipt.external_message_id,
            )

    def _chat_id_from_receipt(self, receipt: Any) -> int:
        """Extract telegram chat_id from a DeliveryReceipt.

        Expects ``external_message_id`` in the form ``'<chat_id>:<message_id>'``
        as produced by ``deliver``.
        """
        ext = receipt.external_message_id
        if ext and ":" in ext:
            return int(ext.split(":", 1)[0])
        raise ValueError(
            f"Cannot extract chat_id from external_message_id={ext!r}; "
            f"expected '<chat_id>:<message_id>' format"
        )

    def _message_id_from_receipt(self, receipt: Any) -> int:
        """Extract telegram message_id from a DeliveryReceipt.

        Expects ``external_message_id`` in the form ``'<chat_id>:<message_id>'``
        as produced by ``deliver``.
        """
        ext = receipt.external_message_id
        if ext and ":" in ext:
            return int(ext.split(":", 1)[1])
        raise ValueError(
            f"Cannot extract message_id from external_message_id={ext!r}; "
            f"expected '<chat_id>:<message_id>' format"
        )

    async def send_typing_indicator(self, target: ChannelTarget) -> None:
        if self._application is None:
            return
        try:
            await self._application.bot.send_chat_action(
                chat_id=int(target.external_chat_id), action="typing"
            )
        except Exception:
            pass  # Non-critical

    # -- Telegram handlers ----------------------------------------------------

    async def _on_start_command(self, update: Any, context: Any) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        await context.bot.send_message(
            chat_id=chat.id,
            text="Hello! I'm your Magi assistant. Send me a message to get started.",
        )

    async def _on_reset_command(self, update: Any, context: Any) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if chat is None or user is None:
            return
        if self._session_mapper is None:
            raise RuntimeError("Telegram channel session mapper is not bound")
        external_chat_id = str(chat.id)
        await self._session_mapper.delete_mapping("telegram", external_chat_id)
        await context.bot.send_message(
            chat_id=chat.id,
            text="Session reset. Your next message will start a new conversation.",
        )

    async def _on_ask_command(self, update: Any, context: Any) -> None:
        """Handle /ask <message> in group or DM."""
        message = update.effective_message
        if message is None or not message.text:
            return
        text = message.text.lstrip("/ask").strip()
        if not text:
            return
        await self._process_inbound(update, text)

    async def _on_message(self, update: Any, context: Any) -> None:
        message = update.effective_message
        if message is None or not message.text:
            return
        chat = update.effective_chat
        if chat is None:
            return

        is_group = chat.type in ("group", "supergroup")
        if is_group:
            if not self._should_process_group(message):
                return
            text = self._strip_mention(message.text)
        else:
            text = message.text

        await self._process_inbound(update, text)

    async def _process_inbound(self, update: Any, text: str) -> None:
        """Common inbound processing for all entry points."""
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if message is None or chat is None or user is None:
            return

        external_user_id = str(user.id)
        if not self._is_user_allowed(external_user_id):
            return
        if self._session_mapper is None:
            raise RuntimeError("Telegram channel session mapper is not bound")
        if self._message_dispatcher is None:
            raise RuntimeError("Telegram channel message dispatcher is not bound")

        is_group = chat.type in ("group", "supergroup")
        display_name = self._build_display_name(chat, user, is_group)

        mapping = await self._session_mapper.resolve_or_create(
            channel_type="telegram",
            external_chat_id=str(chat.id),
            external_user_id=external_user_id,
            is_group=is_group,
            display_name=display_name,
        )

        # Show typing indicator
        try:
            await self._application.bot.send_chat_action(
                chat_id=chat.id, action="typing"
            )
        except Exception:
            pass

        metadata = {
            "channel_type": "telegram",
            "external_chat_id": str(chat.id),
            "external_user_id": external_user_id,
            "external_message_id": str(message.message_id),
            "external_username": user.username,
            "is_group": is_group,
        }

        outcome = await self._message_dispatcher.dispatch_user_message(
            source="telegram",
            user_id=mapping.magi_user_id,
            session_id=mapping.magi_session_id,
            message=text.strip(),
            metadata=metadata,
        )

        if not outcome.success:
            logger.warning(
                "Telegram dispatch failed",
                error_code=outcome.error_code,
                error_message=outcome.error_message,
            )

    # -- Helpers --------------------------------------------------------------

    def _should_process_group(self, message: Any) -> bool:
        """Check if a group message should trigger the bot."""
        text = message.text or ""

        # @mention check
        if message.entities:
            for entity in message.entities:
                if entity.type == "mention":
                    mention_text = text[entity.offset : entity.offset + entity.length]
                    if mention_text.lower() == f"@{self._bot_username.lower()}":
                        return True
                elif entity.type == "text_mention" and entity.user:
                    if entity.user.id == self._bot_id:
                        return True

        # Reply to bot message
        if message.reply_to_message and message.reply_to_message.from_user:
            if message.reply_to_message.from_user.id == self._bot_id:
                return True

        # Keyword trigger
        keyword = self._config.group_trigger_keyword
        if keyword and text.startswith(keyword):
            return True

        return False

    def _strip_mention(self, text: str) -> str:
        """Remove @bot_username from text."""
        if not self._bot_username:
            return text
        return text.replace(f"@{self._bot_username}", "").strip()

    def _is_user_allowed(self, external_user_id: str) -> bool:
        """Check user against whitelist (empty = allow all)."""
        if not self._config.allowed_user_ids:
            return True
        return external_user_id in self._config.allowed_user_ids

    @staticmethod
    def _build_display_name(chat: Any, user: Any, is_group: bool) -> str:
        if is_group:
            return f"TG Group: {chat.title or chat.id}"
        name_parts = [user.first_name or "", user.last_name or ""]
        full_name = " ".join(p for p in name_parts if p).strip()
        if user.username:
            return f"TG: @{user.username}" + (f" ({full_name})" if full_name else "")
        return f"TG: {full_name or user.id}"
