"""Telegram common session commands (/new, /reset) forward to the host's unified
channel-command layer (ONE reset/mapping chain) and surface the host ack — the
plugin no longer resets locally (no _on_reset_command duplicate)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("telegram.ext", reason="python-telegram-bot not installed")

from magi_plugin_sdk.channels import ChannelMessageDispatchOutcome, ChannelSessionMapping

from telegram.adapter import TelegramChannel, TelegramChannelConfig


def _channel() -> TelegramChannel:
    return TelegramChannel(config=TelegramChannelConfig(bot_token="fake", allowed_user_ids=[]))


def _update(text: str):
    msg = MagicMock()
    msg.text = text
    msg.message_id = 7
    chat = MagicMock()
    chat.id = 555
    chat.type = "private"
    user = MagicMock()
    user.id = 42
    user.username = "alice"
    update = MagicMock()
    update.effective_message = msg
    update.effective_chat = chat
    update.effective_user = user
    return update


def _command_dispatcher(*, ack: str | None, turn_id):
    dispatcher = MagicMock()
    dispatcher.dispatch_user_message = AsyncMock(
        return_value=ChannelMessageDispatchOutcome(
            success=True, user_id="local_user", session_id="sess-1",
            turn_id=turn_id, message_id=None, error_message=ack,
        )
    )
    return dispatcher


def _mapper():
    mapper = MagicMock()
    mapper.resolve_or_create = AsyncMock(return_value=ChannelSessionMapping(
        channel_type="telegram", external_chat_id="555",
        magi_session_id="sess-1", magi_user_id="local_user", metadata_json="{}",
    ))
    return mapper


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", ["/reset", "/new"])
async def test_session_command_forwards_raw_to_host_and_surfaces_ack(cmd: str) -> None:
    channel = _channel()
    dispatcher = _command_dispatcher(ack="✨ reset done", turn_id=None)
    channel._message_dispatcher = dispatcher
    mapper = _mapper()
    channel._session_mapper = mapper
    app = MagicMock()
    app.bot = AsyncMock()
    channel._application = app

    await channel._on_session_command(_update(cmd), MagicMock())

    # Raw command text forwarded to the host (host owns the parser + reset chain).
    dispatcher.dispatch_user_message.assert_awaited_once()
    assert dispatcher.dispatch_user_message.await_args.kwargs["message"] == cmd
    # Host ack surfaced to the chat.
    app.bot.send_message.assert_awaited()
    assert app.bot.send_message.await_args.kwargs["text"] == "✨ reset done"
    # No plugin-side reset — the host owns the single mapping chain.
    mapper.delete_mapping.assert_not_called()


@pytest.mark.asyncio
async def test_normal_turn_does_not_surface_command_ack() -> None:
    """A normal turn (turn_id set) replies via deliver(); _process_inbound stays silent."""
    channel = _channel()
    channel._message_dispatcher = _command_dispatcher(ack=None, turn_id="turn_x")
    channel._session_mapper = _mapper()
    app = MagicMock()
    app.bot = AsyncMock()
    channel._application = app

    await channel._process_inbound(_update("hello"), "hello")

    app.bot.send_message.assert_not_called()
