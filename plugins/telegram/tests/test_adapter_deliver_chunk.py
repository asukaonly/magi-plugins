"""Telegram channel streaming contract.

Telegram has no native streaming UX, so it opts OUT of streaming
(``supports_streaming = False``, the SDK default). DeliveryRouter's
``fanout_chunk`` skips non-streaming channels, so chunks never reach
Telegram during streaming. The full assembled text arrives later via the
coordinator's ``fanout_deliver`` → ``Channel.deliver``.

The earlier implementation buffered chunks then called ``self.deliver``
on ``is_final=True``. That caused double-delivery because the
coordinator's ``fanout_deliver`` also fired with the same assembled text.
This test pins the corrected contract so a regression of either
``supports_streaming`` or an accidental ``deliver_chunk`` override
would fail loudly.
"""
from __future__ import annotations

import asyncio

import pytest

from magi_plugin_sdk.channels import ChannelTarget
from magi_plugin_sdk.delivery import DeliveryChunk

from telegram.adapter import TelegramChannel, TelegramChannelConfig


def _make_channel() -> TelegramChannel:
    return TelegramChannel(
        config=TelegramChannelConfig(bot_token="dummy"),
        session_mapper=None,
        message_dispatcher=None,
    )


def test_supports_streaming_is_false():
    """Telegram opts out of streaming — DeliveryRouter must skip it for
    chunk fanout so the final ``deliver()`` is the only send path."""
    ch = _make_channel()
    assert ch.supports_streaming is False


def test_deliver_chunk_inherits_sdk_default_raise():
    """Telegram does not override ``deliver_chunk``; the SDK base raises
    NotImplementedError if anyone bypasses the ``supports_streaming`` gate.

    This is defense-in-depth: if a future caller accidentally invokes
    ``channel.deliver_chunk`` directly (skipping ``fanout_chunk``'s gate),
    the failure is loud rather than silent.
    """
    ch = _make_channel()
    target = ChannelTarget(channel_type="telegram", external_chat_id="42")
    chunk = DeliveryChunk(text="hi", is_final=False, seq=0)

    with pytest.raises(NotImplementedError, match="TelegramChannel"):
        asyncio.run(ch.deliver_chunk(target, chunk))


def test_no_chunk_buffer_field():
    """The legacy ``_chunk_buffers`` attribute is gone — confirms cleanup landed."""
    ch = _make_channel()
    assert not hasattr(ch, "_chunk_buffers")
