"""Telegram ``deliver_control_request`` + callback round-trip — CF-9.

Pins:
* ``supports_control_requests`` is True (host fanout includes us).
* ``deliver_control_request`` looks up chat_id from session_mapper
  by magi_session_id, sends a message with InlineKeyboardMarkup
  carrying ``magi:approve:<short_id>`` / ``magi:deny:<short_id>``
  callback_data on the two buttons.
* The button-tap callback handler translates callback_data into a
  synthesized ``/approve {short_id}`` / ``/deny {short_id}`` text
  message dispatched through the normal inbound path — the host's
  CF-6 slash-command parser owns the broker resolution.
* Unknown button data (not magi: prefix) is ignored without crashing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from magi_plugin_sdk import ControlRequest

# python-telegram-bot may not be installed in the test environment.
# Tests that exercise actual telegram-side API construction skip.
pytest.importorskip("telegram.ext", reason="python-telegram-bot not installed")
from magi_plugin_sdk.channels import (
    ChannelMessageDispatchOutcome,
    ChannelSessionMapping,
    ChannelTarget,
)

from telegram.adapter import TelegramChannel, TelegramChannelConfig


def _make_channel(*, allowed_user_ids: list[str] | None = None) -> TelegramChannel:
    config = TelegramChannelConfig(
        bot_token="fake-token", allowed_user_ids=allowed_user_ids or [],
    )
    return TelegramChannel(config=config)


def _make_request(*, short_id: str = "abc123") -> ControlRequest:
    return ControlRequest(
        request_id=f"01HFTGSM7Z8X9YQK4PVAN3{short_id.upper()}",
        short_id=short_id,
        kind="permission",
        tool_name="image_gen",
        preview="Generate a cat",
    )


# === Capability flag =====================================================


def test_supports_control_requests_is_true() -> None:
    """Phase H+2 opt-in flag — class-level so the host can read it
    without instantiating."""
    assert TelegramChannel.supports_control_requests is True


# === deliver_control_request ============================================


@pytest.mark.asyncio
async def test_deliver_control_request_sends_message_with_inline_keyboard() -> None:
    channel = _make_channel()
    # Stub the application + session_mapper
    bot = AsyncMock()
    application = MagicMock()
    application.bot = bot
    channel._application = application

    mapper = MagicMock()
    mapper.lookup_by_session = AsyncMock(return_value=ChannelSessionMapping(
        channel_type="telegram",
        external_chat_id="555",
        magi_session_id="sess-1",
        magi_user_id="local_user",
        metadata_json="{}",
    ))
    channel._session_mapper = mapper

    target = ChannelTarget(
        channel_type="telegram",
        external_chat_id="",
        magi_session_id="sess-1",
        magi_user_id="local_user",
    )
    request = _make_request(short_id="abc123")
    await channel.deliver_control_request(target, request)

    bot.send_message.assert_awaited()
    call = bot.send_message.await_args
    assert call.kwargs["chat_id"] == 555
    assert "image_gen" in call.kwargs["text"]
    assert "abc123" in call.kwargs["text"]

    keyboard = call.kwargs["reply_markup"]
    # InlineKeyboardMarkup is a one-row, two-button layout.
    flat_buttons = [btn for row in keyboard.inline_keyboard for btn in row]
    callbacks = {btn.callback_data for btn in flat_buttons}
    assert callbacks == {"magi:approve:abc123", "magi:deny:abc123"}


@pytest.mark.asyncio
async def test_deliver_control_request_no_mapping_silent() -> None:
    """If the session has no Telegram mapping, the channel can't
    deliver — silently no-op rather than crash."""
    channel = _make_channel()
    bot = AsyncMock()
    application = MagicMock()
    application.bot = bot
    channel._application = application

    mapper = MagicMock()
    mapper.lookup_by_session = AsyncMock(return_value=None)
    channel._session_mapper = mapper

    target = ChannelTarget(
        channel_type="telegram", external_chat_id="",
        magi_session_id="sess-without-telegram",
        magi_user_id="local_user",
    )
    await channel.deliver_control_request(target, _make_request())
    bot.send_message.assert_not_called()


# === Callback handler ====================================================


@dataclass
class _FakeUpdate:
    callback_query: Any
    effective_chat: Any
    effective_user: Any


@dataclass
class _FakeChat:
    id: int = 555
    type: str = "private"


@dataclass
class _FakeUser:
    id: int = 42
    username: str = "alice"


@pytest.mark.asyncio
async def test_callback_query_dispatches_synthesized_slash_approve() -> None:
    channel = _make_channel()

    dispatcher = MagicMock()
    dispatcher.dispatch_user_message = AsyncMock(return_value=ChannelMessageDispatchOutcome(
        success=True, user_id="local_user", session_id="sess-1",
        turn_id=None, message_id=None, error_message="✓ 已同意工具 image_gen (abc123)",
    ))
    channel._message_dispatcher = dispatcher

    mapper = MagicMock()
    mapper.lookup = AsyncMock(return_value=ChannelSessionMapping(
        channel_type="telegram", external_chat_id="555",
        magi_session_id="sess-1", magi_user_id="local_user",
        metadata_json='{"external_user_id":"42"}',
    ))
    channel._session_mapper = mapper

    query = MagicMock()
    query.data = "magi:approve:abc123"
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    update = _FakeUpdate(
        callback_query=query, effective_chat=_FakeChat(), effective_user=_FakeUser(),
    )
    context = MagicMock()
    context.bot.send_message = AsyncMock()

    await channel._on_callback_query(update, context)

    # Synthesized /approve dispatched
    dispatcher.dispatch_user_message.assert_awaited_once()
    kwargs = dispatcher.dispatch_user_message.await_args.kwargs
    assert kwargs["source"] == "telegram"
    assert kwargs["message"] == "/approve abc123"
    assert kwargs["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_callback_query_dispatches_synthesized_slash_deny() -> None:
    channel = _make_channel()
    dispatcher = MagicMock()
    dispatcher.dispatch_user_message = AsyncMock(return_value=ChannelMessageDispatchOutcome(
        success=True, user_id="local_user", session_id="sess-1",
        turn_id=None, message_id=None, error_message=None,
    ))
    channel._message_dispatcher = dispatcher
    mapper = MagicMock()
    mapper.lookup = AsyncMock(return_value=ChannelSessionMapping(
        channel_type="telegram", external_chat_id="555",
        magi_session_id="sess-1", magi_user_id="local_user",
        metadata_json="{}",
    ))
    channel._session_mapper = mapper

    query = MagicMock()
    query.data = "magi:deny:xyz789"
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    update = _FakeUpdate(
        callback_query=query, effective_chat=_FakeChat(), effective_user=_FakeUser(),
    )
    context = MagicMock()
    context.bot.send_message = AsyncMock()

    await channel._on_callback_query(update, context)
    kwargs = dispatcher.dispatch_user_message.await_args.kwargs
    assert kwargs["message"] == "/deny xyz789"


@pytest.mark.asyncio
async def test_callback_query_ignores_non_magi_data() -> None:
    """Other plugins (or future Telegram features) may use callback
    queries — ignore data that doesn't match our prefix."""
    channel = _make_channel()
    dispatcher = MagicMock()
    dispatcher.dispatch_user_message = AsyncMock()
    channel._message_dispatcher = dispatcher
    channel._session_mapper = MagicMock()

    query = MagicMock()
    query.data = "other_plugin:something:foo"
    query.answer = AsyncMock()
    update = _FakeUpdate(
        callback_query=query, effective_chat=_FakeChat(), effective_user=_FakeUser(),
    )
    await channel._on_callback_query(update, MagicMock())
    dispatcher.dispatch_user_message.assert_not_called()


@pytest.mark.asyncio
async def test_callback_query_disallowed_user_rejected() -> None:
    """allowed_user_ids gate also applies to button taps — a stranger
    can't approve via someone else's bot."""
    channel = _make_channel(allowed_user_ids=["1", "2", "3"])  # not 42
    dispatcher = MagicMock()
    dispatcher.dispatch_user_message = AsyncMock()
    channel._message_dispatcher = dispatcher
    channel._session_mapper = MagicMock()

    query = MagicMock()
    query.data = "magi:approve:abc123"
    query.answer = AsyncMock()
    update = _FakeUpdate(
        callback_query=query, effective_chat=_FakeChat(), effective_user=_FakeUser(),
    )
    await channel._on_callback_query(update, MagicMock())
    dispatcher.dispatch_user_message.assert_not_called()
