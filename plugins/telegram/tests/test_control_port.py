"""Telegram routes control commands through the host control port.

/reset, /new, and the /approve button all invoke the host's unified
ChannelControlPort and surface its typed ack — no message-dispatch round-trip,
no error_message overload. Non-commands fall through to normal dispatch.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("telegram.ext", reason="python-telegram-bot not installed")

from magi_plugin_sdk.channels import (  # noqa: E402
    ChannelControlCommandResult,
    ChannelInboundContext,
    ChannelMessageDispatchOutcome,
    ChannelProviderTimeEvidence,
    ChannelSessionMapping,
)

from telegram.adapter import TelegramChannel, TelegramChannelConfig  # noqa: E402


def _channel() -> TelegramChannel:
    return TelegramChannel(config=TelegramChannelConfig(bot_token="fake", allowed_user_ids=[]))


def _msg_update(text: str):
    msg = MagicMock()
    msg.text = text
    msg.message_id = 7
    msg.date = datetime(2026, 8, 1, tzinfo=UTC)
    chat = MagicMock()
    chat.id = 555
    chat.type = "private"
    user = MagicMock()
    user.id = 42
    user.username = "alice"
    user.first_name = "Alice"
    user.last_name = "Example"
    update = MagicMock()
    update.effective_message = msg
    update.effective_chat = chat
    update.effective_user = user
    return update


def _port(result):
    p = MagicMock()
    p.handle_command = AsyncMock(return_value=result)
    return p


def _mapper():
    m = MagicMock()
    mapping = ChannelSessionMapping("telegram", "555", "sess-1", "local_user")
    m.resolve_or_create = AsyncMock(return_value=mapping)
    m.lookup = AsyncMock(return_value=mapping)
    return m


def _dispatcher():
    d = MagicMock()
    d.capture_inbound_context = AsyncMock(
        return_value=ChannelInboundContext(
            channel_type="telegram",
            stream_id="555",
            admission_evidence=ChannelProviderTimeEvidence(
                provider_occurred_at_ms=1_754_006_400_000,
            ),
            clear_generation=0,
        )
    )
    d.dispatch_user_message = AsyncMock(
        return_value=ChannelMessageDispatchOutcome(
            success=True,
            user_id="local_user",
            session_id="sess-1",
            turn_id="t",
            message_id="m",
        )
    )
    return d


def _wire(ch, *, result):
    ch._session_mapper = _mapper()
    ch._message_dispatcher = _dispatcher()
    ch._control_port = _port(result)
    app = MagicMock()
    app.bot = AsyncMock()
    ch._application = app
    return app


@pytest.mark.asyncio
async def test_process_inbound_command_via_port_acked_not_dispatched() -> None:
    ch = _channel()
    app = _wire(ch, result=ChannelControlCommandResult(ack="✨ 已重置", kind="session"))
    await ch._process_inbound(_msg_update("/reset"), "/reset")
    ch._control_port.handle_command.assert_awaited_once()
    assert ch._control_port.handle_command.await_args.kwargs["message"] == "/reset"
    app.bot.send_message.assert_awaited()
    assert app.bot.send_message.await_args.kwargs["text"] == "✨ 已重置"
    ch._message_dispatcher.dispatch_user_message.assert_not_called()


@pytest.mark.asyncio
async def test_process_inbound_non_command_dispatches() -> None:
    ch = _channel()
    _wire(ch, result=None)
    await ch._process_inbound(_msg_update("hello"), "hello")
    ch._control_port.handle_command.assert_awaited_once()
    ch._message_dispatcher.dispatch_user_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_query_approve_via_port() -> None:
    ch = _channel()
    _wire(ch, result=ChannelControlCommandResult(ack="✓ 已同意", kind="permission"))

    query = MagicMock()
    query.data = "magi:approve:abc123"
    query.message = MagicMock()
    query.message.date = datetime(2026, 8, 1, tzinfo=UTC)
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    chat = MagicMock()
    chat.id = 555
    chat.type = "private"
    user = MagicMock()
    user.id = 42
    user.username = "alice"
    user.first_name = "Alice"
    user.last_name = "Example"
    update = MagicMock()
    update.callback_query = query
    update.effective_chat = chat
    update.effective_user = user
    ctx = MagicMock()
    ctx.bot = AsyncMock()

    await ch._on_callback_query(update, ctx)

    ch._control_port.handle_command.assert_awaited_once()
    assert ch._control_port.handle_command.await_args.kwargs["message"] == "/approve abc123"
    assert ctx.bot.send_message.await_args.kwargs["text"] == "✓ 已同意"
    ch._message_dispatcher.dispatch_user_message.assert_not_called()
