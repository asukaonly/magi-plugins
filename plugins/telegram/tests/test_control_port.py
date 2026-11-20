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

from magi_telegram_plugin_test.adapter import TelegramChannel, TelegramChannelConfig  # noqa: E402


def _channel() -> TelegramChannel:
    return TelegramChannel(config=TelegramChannelConfig(bot_token="fake", allowed_user_ids=[]))


def _msg_update(text: str):
    msg = MagicMock(message_thread_id=None)
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
@pytest.mark.parametrize("thread_id", [11, 22])
async def test_topic_identity_reaches_host_binding_and_typing(thread_id: int) -> None:
    ch = _channel()
    app = _wire(ch, result=None)
    update = _msg_update("hello")
    update.effective_message.message_thread_id = thread_id
    await ch._process_inbound(update, "hello")
    assert ch._session_mapper.resolve_or_create.await_args.kwargs["external_thread_id"] == str(thread_id)
    assert ch._message_dispatcher.dispatch_user_message.await_args.kwargs["metadata"]["external_thread_id"] == str(thread_id)
    assert app.bot.send_chat_action.await_args.kwargs["message_thread_id"] == thread_id


@pytest.mark.asyncio
async def test_callback_query_approve_via_port() -> None:
    ch = _channel()
    _wire(ch, result=ChannelControlCommandResult(ack="✓ 已同意", kind="permission"))

    query = MagicMock()
    query.data = "magi:approve:abc123"
    query.message = MagicMock(message_thread_id=None)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["help", "new", "reset"])
async def test_registered_commands_forward_only_requests_for_this_bot(monkeypatch, command):
    from telegram import Update
    from telegram.ext import Application, CommandHandler

    channel = _channel()
    app = _wire(channel, result=ChannelControlCommandResult(ack="Host reply", kind="help"))
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.updater.start_polling = AsyncMock()
    app.bot.defaults = None
    app.bot.username = "MagiBot"
    app.bot.get_me.return_value.username = "MagiBot"
    builder = MagicMock()
    builder.token.return_value.build.return_value = app
    monkeypatch.setattr(Application, "builder", lambda: builder)
    await channel.start()
    handler, = (
        call.args[0] for call in app.add_handler.call_args_list
        if isinstance(call.args[0], CommandHandler) and command in call.args[0].commands
    )

    for suffix in ("", "@magibot", "@OtherBot"):
        text = f"/{command}{suffix}"
        update = Update.de_json({
            "update_id": 1,
            "message": {
                "message_id": 7, "date": 1_754_006_400,
                "chat": {"id": 555, "type": "supergroup", "title": "Group"},
                "from": {"id": 42, "is_bot": False, "first_name": "Alice"},
                "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
            },
        }, app.bot)
        matched = handler.check_update(update)
        if suffix == "@OtherBot":
            assert not matched
            continue
        assert matched
        await handler.callback(update, MagicMock(args=matched[0]))
        assert channel._control_port.handle_command.await_args.kwargs["message"] == f"/{command}"
        assert app.bot.send_message.await_args.kwargs["text"] == "Host reply"
    assert channel._control_port.handle_command.await_count == 2
    channel._message_dispatcher.dispatch_user_message.assert_not_awaited()
