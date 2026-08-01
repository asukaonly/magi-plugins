"""Telegram inbound clear admission and provider-time evidence."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from magi_plugin_sdk.channels import (
    ChannelControlCommandResult,
    ChannelInboundClearRequest,
    ChannelInboundClearStrategy,
    ChannelInboundContext,
    ChannelInboundRejectedError,
    ChannelInboundRejectionReason,
    ChannelMessageDispatchOutcome,
    ChannelProviderTimeEvidence,
    ChannelSessionMapping,
)

from telegram.adapter import TelegramChannel, TelegramChannelConfig


PROVIDER_DATE = datetime(2026, 8, 1, 3, 4, 5, tzinfo=UTC)
PROVIDER_TIME_MS = int(PROVIDER_DATE.timestamp() * 1000)


def _update(text: str, *, provider_date: object = PROVIDER_DATE) -> SimpleNamespace:
    message = SimpleNamespace(
        text=text,
        message_id=7,
        date=provider_date,
        entities=[],
        reply_to_message=None,
    )
    chat = SimpleNamespace(id=555, type="private", title=None)
    user = SimpleNamespace(
        id=42,
        username="alice",
        first_name="Alice",
        last_name="Example",
    )
    return SimpleNamespace(
        effective_message=message,
        effective_chat=chat,
        effective_user=user,
    )


def _inbound_context(
    *,
    generation: int = 0,
    occurred_at_ms: int = PROVIDER_TIME_MS,
) -> ChannelInboundContext:
    return ChannelInboundContext(
        channel_type="telegram",
        stream_id="555",
        admission_evidence=ChannelProviderTimeEvidence(
            provider_occurred_at_ms=occurred_at_ms,
        ),
        clear_generation=generation,
    )


def _wired_channel(
    *,
    context: ChannelInboundContext | None = None,
    control_result: ChannelControlCommandResult | None = None,
) -> tuple[TelegramChannel, MagicMock, MagicMock, MagicMock, MagicMock]:
    channel = TelegramChannel(
        config=TelegramChannelConfig(bot_token="fake", allowed_user_ids=[]),
    )
    inbound_context = context or _inbound_context()
    dispatcher = MagicMock()
    dispatcher.capture_inbound_context = AsyncMock(return_value=inbound_context)
    dispatcher.dispatch_user_message = AsyncMock(
        return_value=ChannelMessageDispatchOutcome(
            success=True,
            user_id="local_user",
            session_id="sess-1",
            turn_id="turn-1",
            message_id="message-1",
        )
    )
    mapper = MagicMock()
    mapper.resolve_or_create = AsyncMock(
        return_value=ChannelSessionMapping(
            channel_type="telegram",
            external_chat_id="555",
            magi_session_id="sess-1",
            magi_user_id="local_user",
        )
    )
    mapper.lookup = AsyncMock(return_value=mapper.resolve_or_create.return_value)
    control_port = MagicMock()
    control_port.handle_command = AsyncMock(return_value=control_result)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_chat_action = AsyncMock()
    channel._message_dispatcher = dispatcher
    channel._session_mapper = mapper
    channel._control_port = control_port
    channel._application = SimpleNamespace(bot=bot)
    return channel, dispatcher, mapper, control_port, bot


def test_telegram_declares_provider_time_clear_strategy() -> None:
    assert (
        TelegramChannel.inbound_clear_strategy
        is ChannelInboundClearStrategy.PROVIDER_TIME
    )


@pytest.mark.asyncio
async def test_message_passes_one_provider_context_through_every_host_call() -> None:
    channel, dispatcher, mapper, control_port, bot = _wired_channel()
    inbound_context = dispatcher.capture_inbound_context.return_value
    order: list[str] = []

    async def capture(**_kwargs):
        order.append("capture")
        return inbound_context

    async def resolve(**_kwargs):
        order.append("mapping")
        return mapper.resolve_or_create.return_value

    async def control(**_kwargs):
        order.append("control")
        return None

    async def typing(**_kwargs):
        order.append("typing")

    async def dispatch(**_kwargs):
        order.append("dispatch")
        return ChannelMessageDispatchOutcome(
            success=True,
            user_id="local_user",
            session_id="sess-1",
        )

    dispatcher.capture_inbound_context.side_effect = capture
    mapper.resolve_or_create.side_effect = resolve
    control_port.handle_command.side_effect = control
    bot.send_chat_action.side_effect = typing
    dispatcher.dispatch_user_message.side_effect = dispatch

    await channel._process_inbound(_update("hello"), "hello")

    assert order == ["capture", "mapping", "control", "typing", "dispatch"]
    capture_kwargs = dispatcher.capture_inbound_context.await_args.kwargs
    assert capture_kwargs == {
        "channel_type": "telegram",
        "stream_id": "555",
        "evidence": ChannelProviderTimeEvidence(
            provider_occurred_at_ms=PROVIDER_TIME_MS
        ),
    }
    assert mapper.resolve_or_create.await_args.kwargs["inbound_context"] is inbound_context
    assert control_port.handle_command.await_args.kwargs["inbound_context"] is inbound_context
    assert dispatcher.dispatch_user_message.await_args.kwargs["inbound_context"] is inbound_context


@pytest.mark.asyncio
async def test_command_uses_provider_time_before_mapping_control_and_ack() -> None:
    result = ChannelControlCommandResult(ack="reset done", kind="session")
    channel, dispatcher, mapper, control_port, bot = _wired_channel(
        control_result=result
    )
    inbound_context = dispatcher.capture_inbound_context.return_value

    await channel._process_inbound(_update("/reset"), "/reset")

    evidence = dispatcher.capture_inbound_context.await_args.kwargs["evidence"]
    assert evidence.provider_occurred_at_ms == PROVIDER_TIME_MS
    assert mapper.resolve_or_create.await_args.kwargs["inbound_context"] is inbound_context
    assert control_port.handle_command.await_args.kwargs["inbound_context"] is inbound_context
    bot.send_message.assert_awaited_once_with(chat_id=555, text="reset done")
    bot.send_chat_action.assert_not_awaited()
    dispatcher.dispatch_user_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_uses_callback_message_provider_time_and_same_context() -> None:
    result = ChannelControlCommandResult(ack="approved", kind="permission")
    channel, dispatcher, mapper, control_port, _ = _wired_channel(
        control_result=result
    )
    inbound_context = dispatcher.capture_inbound_context.return_value
    query = SimpleNamespace(
        data="magi:approve:abc123",
        message=SimpleNamespace(date=PROVIDER_DATE),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    callback_bot = MagicMock()
    callback_bot.send_message = AsyncMock()
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=555, type="private", title=None),
        effective_user=SimpleNamespace(
            id=42,
            username="alice",
            first_name="Alice",
            last_name="Example",
        ),
        effective_message=SimpleNamespace(date=None),
    )

    await channel._on_callback_query(
        update,
        SimpleNamespace(bot=callback_bot),
    )

    evidence = dispatcher.capture_inbound_context.await_args.kwargs["evidence"]
    assert evidence.provider_occurred_at_ms == PROVIDER_TIME_MS
    mapper.lookup.assert_awaited_once_with("telegram", "555")
    mapper.resolve_or_create.assert_not_awaited()
    assert control_port.handle_command.await_args.kwargs["inbound_context"] is inbound_context
    query.answer.assert_awaited_once()
    callback_bot.send_message.assert_awaited_once_with(
        chat_id=555,
        text="approved",
    )


@pytest.mark.parametrize(
    "provider_date",
    [
        None,
        "2026-08-01T03:04:05Z",
        datetime(2026, 8, 1, 3, 4, 5),
        datetime(1970, 1, 1, tzinfo=UTC),
    ],
)
@pytest.mark.parametrize("text", ["hello", "/reset"])
@pytest.mark.asyncio
async def test_invalid_message_or_command_time_is_terminal_without_side_effects(
    provider_date: object,
    text: str,
) -> None:
    channel, dispatcher, mapper, control_port, bot = _wired_channel()

    await channel._process_inbound(
        _update(text, provider_date=provider_date),
        text,
    )

    dispatcher.capture_inbound_context.assert_not_awaited()
    mapper.lookup.assert_not_awaited()
    mapper.resolve_or_create.assert_not_awaited()
    control_port.handle_command.assert_not_awaited()
    dispatcher.dispatch_user_message.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    bot.send_chat_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_callback_time_does_not_ack_or_touch_host_state() -> None:
    channel, dispatcher, mapper, control_port, _ = _wired_channel()
    query = SimpleNamespace(
        data="magi:deny:abc123",
        message=SimpleNamespace(date=None),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    callback_bot = MagicMock()
    callback_bot.send_message = AsyncMock()
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=555, type="private", title=None),
        effective_user=SimpleNamespace(
            id=42,
            username="alice",
            first_name="Alice",
            last_name="Example",
        ),
    )

    await channel._on_callback_query(update, SimpleNamespace(bot=callback_bot))

    dispatcher.capture_inbound_context.assert_not_awaited()
    mapper.resolve_or_create.assert_not_awaited()
    control_port.handle_command.assert_not_awaited()
    query.answer.assert_not_awaited()
    query.edit_message_reply_markup.assert_not_awaited()
    callback_bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_host_rejection_is_terminal_without_mapping_ack_typing_or_dispatch() -> None:
    channel, dispatcher, mapper, control_port, bot = _wired_channel()
    dispatcher.capture_inbound_context.side_effect = ChannelInboundRejectedError(
        ChannelInboundRejectionReason.CLEARED_MESSAGE,
        "old provider event",
    )

    await channel._process_inbound(_update("old message"), "old message")

    dispatcher.capture_inbound_context.assert_awaited_once()
    mapper.resolve_or_create.assert_not_awaited()
    control_port.handle_command.assert_not_awaited()
    dispatcher.dispatch_user_message.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    bot.send_chat_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_pauses_new_ingress_waits_local_activity_and_fences_old_context() -> None:
    channel, dispatcher, mapper, control_port, bot = _wired_channel()
    typing_started = asyncio.Event()
    release_typing = asyncio.Event()
    clear_entered = asyncio.Event()
    release_clear = asyncio.Event()
    capture_count = 0
    host_generation = 0
    accepted_messages: list[str] = []

    async def capture(**kwargs):
        nonlocal capture_count
        capture_count += 1
        evidence = kwargs["evidence"]
        return ChannelInboundContext(
            channel_type="telegram",
            stream_id="555",
            admission_evidence=evidence,
            clear_generation=host_generation,
        )

    async def typing(**_kwargs):
        if not typing_started.is_set():
            typing_started.set()
            await release_typing.wait()

    async def dispatch(**kwargs):
        inbound_context = kwargs["inbound_context"]
        if (
            inbound_context.clear_generation
            != channel._applied_inbound_clear_generation
        ):
            raise ChannelInboundRejectedError(
                ChannelInboundRejectionReason.CLEARED_MESSAGE,
                "old provider event",
            )
        accepted_messages.append(kwargs["message"])
        return ChannelMessageDispatchOutcome(
            success=True,
            user_id="local_user",
            session_id="sess-1",
        )

    dispatcher.capture_inbound_context.side_effect = capture
    dispatcher.dispatch_user_message.side_effect = dispatch
    control_port.handle_command.return_value = None
    bot.send_chat_action.side_effect = typing

    async def run_clear() -> None:
        nonlocal host_generation
        host_generation = 1
        async with channel.inbound_clear_boundary(
            ChannelInboundClearRequest(
                channel_type="telegram",
                clear_generation=1,
            )
        ):
            clear_entered.set()
            await release_clear.wait()

    first = asyncio.create_task(
        channel._process_inbound(_update("first"), "first")
    )
    await asyncio.wait_for(typing_started.wait(), timeout=1)
    clear_task = asyncio.create_task(run_clear())
    while not channel._inbound_clear_active:
        await asyncio.sleep(0)
    assert not clear_entered.is_set()

    second = asyncio.create_task(
        channel._process_inbound(_update("second"), "second")
    )
    await asyncio.sleep(0)
    assert capture_count == 1

    release_typing.set()
    await asyncio.wait_for(clear_entered.wait(), timeout=1)
    await asyncio.wait_for(first, timeout=1)
    assert accepted_messages == []
    assert capture_count == 1

    release_clear.set()
    await asyncio.wait_for(clear_task, timeout=1)
    await asyncio.wait_for(second, timeout=1)
    assert accepted_messages == ["second"]
    assert capture_count == 2


@pytest.mark.asyncio
async def test_restart_accepts_the_hosts_current_clear_generation() -> None:
    channel, dispatcher, _, _, _ = _wired_channel(
        context=_inbound_context(generation=7),
    )

    await channel._process_inbound(_update("after restart"), "after restart")

    dispatcher.dispatch_user_message.assert_awaited_once()
    assert channel._applied_inbound_clear_generation == 7


@pytest.mark.asyncio
async def test_same_process_rejects_context_captured_before_clear_boundary() -> None:
    channel, _, _, _, _ = _wired_channel()
    old_context = _inbound_context(generation=4)

    async with channel._local_inbound_activity(old_context):
        pass

    async with channel.inbound_clear_boundary(
        ChannelInboundClearRequest(
            channel_type="telegram",
            clear_generation=5,
        )
    ):
        pass

    with pytest.raises(ChannelInboundRejectedError) as exc_info:
        async with channel._local_inbound_activity(old_context):
            pass

    assert exc_info.value.reason is ChannelInboundRejectionReason.CLEARED_MESSAGE


@pytest.mark.asyncio
async def test_inbound_clear_boundary_is_local_only() -> None:
    channel, dispatcher, _, _, bot = _wired_channel()

    async with channel.inbound_clear_boundary(
        ChannelInboundClearRequest(
            channel_type="telegram",
            clear_generation=3,
        )
    ):
        assert channel._inbound_clear_active is True
        assert channel._applied_inbound_clear_generation == 3

    dispatcher.capture_inbound_context.assert_not_awaited()
    dispatcher.dispatch_user_message.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    bot.send_chat_action.assert_not_awaited()
