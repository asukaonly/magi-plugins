from __future__ import annotations

import pytest

from magi_plugin_sdk.channels import ChannelTarget
from magi_plugin_sdk.delivery import DeliveryChunk, DeliveryContent, DeliveryReceipt

from telegram.adapter import TelegramChannel, TelegramChannelConfig


class _RecordingChannel(TelegramChannel):
    """TelegramChannel with deliver() stubbed to record calls."""

    def __init__(self, *, config):
        super().__init__(config=config, session_mapper=None, message_dispatcher=None)
        self.deliver_calls = []

    async def deliver(self, target, content):
        self.deliver_calls.append((target, content))
        return DeliveryReceipt(
            channel_id=f"{target.channel_type}:{target.external_chat_id}",
            external_message_id="stub:1",
            delivered_at_ms=0,
        )


def _make_channel():
    return _RecordingChannel(config=TelegramChannelConfig(bot_token="dummy"))


@pytest.mark.asyncio
async def test_supports_streaming_is_true():
    ch = _make_channel()
    assert ch.supports_streaming is True


@pytest.mark.asyncio
async def test_deliver_chunk_buffers_until_final_then_sends_once():
    ch = _make_channel()
    target = ChannelTarget(channel_type="telegram", external_chat_id="42")

    await ch.deliver_chunk(target, DeliveryChunk(text="he", is_final=False, seq=0))
    await ch.deliver_chunk(target, DeliveryChunk(text="llo", is_final=False, seq=1))
    await ch.deliver_chunk(target, DeliveryChunk(text=" world", is_final=True, seq=2))

    assert len(ch.deliver_calls) == 1
    _, content = ch.deliver_calls[0]
    assert content.text == "hello world"


@pytest.mark.asyncio
async def test_deliver_chunk_isolates_per_target_chat_id():
    ch = _make_channel()
    t_a = ChannelTarget(channel_type="telegram", external_chat_id="42")
    t_b = ChannelTarget(channel_type="telegram", external_chat_id="99")

    await ch.deliver_chunk(t_a, DeliveryChunk(text="A1 ", is_final=False, seq=0))
    await ch.deliver_chunk(t_b, DeliveryChunk(text="B1 ", is_final=False, seq=0))
    await ch.deliver_chunk(t_a, DeliveryChunk(text="A2", is_final=True, seq=1))
    await ch.deliver_chunk(t_b, DeliveryChunk(text="B2", is_final=True, seq=1))

    assert len(ch.deliver_calls) == 2
    chats = {target.external_chat_id: content.text for target, content in ch.deliver_calls}
    assert chats == {"42": "A1 A2", "99": "B1 B2"}


@pytest.mark.asyncio
async def test_deliver_chunk_final_with_empty_text_sends_buffered_only():
    """A boundary chunk with text='' still triggers the send of buffered text."""
    ch = _make_channel()
    target = ChannelTarget(channel_type="telegram", external_chat_id="42")

    await ch.deliver_chunk(target, DeliveryChunk(text="hi", is_final=False, seq=0))
    await ch.deliver_chunk(target, DeliveryChunk(text="", is_final=True, seq=1))

    assert len(ch.deliver_calls) == 1
    assert ch.deliver_calls[0][1].text == "hi"


@pytest.mark.asyncio
async def test_deliver_chunk_with_only_final_empty_does_not_send():
    """Boundary-only call with no buffered text and empty final → no send."""
    ch = _make_channel()
    target = ChannelTarget(channel_type="telegram", external_chat_id="42")
    await ch.deliver_chunk(target, DeliveryChunk(text="", is_final=True, seq=0))
    assert ch.deliver_calls == []
