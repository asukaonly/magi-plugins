"""Phase A media-outbound: Telegram ``deliver`` sends attachments after text.

Pin the routing decisions so a regression of the kind→method mapping or
the caption-vs-preamble heuristic fails loudly:

* No attachments → text-only `send_message` (legacy path unchanged).
* 1 image, short text → 1 `send_photo` with caption=text. No second message.
* 1 image, long text  → `send_message(text)` first, then `send_photo` with no caption.
* image + document    → `send_photo` + `send_document`, only first carries caption.
* gif mime / animation kind → `send_animation` (not `send_photo`).
* unknown kind → falls through to `send_document` (safe catch-all).

The receipt's ``external_message_id`` MUST track the LAST sent surface
so a subsequent retract operates on the most recent message in the chat.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from magi_plugin_sdk.channels import ChannelTarget
from magi_plugin_sdk.delivery import DeliveryContent

from telegram.adapter import TelegramChannel, TelegramChannelConfig


def _make_channel(tmp_path: Path):
    """Build a channel with stubbed PTB application/bot.

    All bot.send_* methods are AsyncMocks returning a Message whose
    ``message_id`` is a monotonically increasing int — that lets us
    pin "receipt ends up on the LAST send" with a numeric assertion.
    """
    channel = TelegramChannel(config=TelegramChannelConfig(bot_token="dummy"))

    counter = {"i": 0}

    def _make_message():
        counter["i"] += 1
        return MagicMock(message_id=counter["i"])

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=lambda **kwargs: _make_message())
    bot.send_photo = AsyncMock(side_effect=lambda **kwargs: _make_message())
    bot.send_video = AsyncMock(side_effect=lambda **kwargs: _make_message())
    bot.send_document = AsyncMock(side_effect=lambda **kwargs: _make_message())
    bot.send_animation = AsyncMock(side_effect=lambda **kwargs: _make_message())
    bot.send_audio = AsyncMock(side_effect=lambda **kwargs: _make_message())
    bot.send_voice = AsyncMock(side_effect=lambda **kwargs: _make_message())

    app = MagicMock()
    app.bot = bot
    channel._application = app
    return channel, bot


def _attachment(tmp_path: Path, name: str, kind: str, mime: str) -> dict:
    p = tmp_path / name
    p.write_bytes(b"binary-payload-" + name.encode())
    return {
        "attachment_id": f"att-{name}",
        "kind": kind,
        "original_name": name,
        "mime_type": mime,
        "size_bytes": p.stat().st_size,
        "storage_path": str(p),
        "sha256": "fakehash",
    }


def _target() -> ChannelTarget:
    return ChannelTarget(
        channel_type="telegram",
        external_chat_id="42",
        magi_session_id="s-1",
        magi_user_id="u-1",
    )


@pytest.mark.asyncio
async def test_text_only_unchanged_legacy_path(tmp_path):
    channel, bot = _make_channel(tmp_path)
    receipt = await channel.deliver(_target(), DeliveryContent(text="hello"))
    bot.send_message.assert_awaited_once()
    bot.send_photo.assert_not_awaited()
    bot.send_document.assert_not_awaited()
    assert receipt.external_message_id == "42:1"


@pytest.mark.asyncio
async def test_single_image_short_text_carries_caption(tmp_path):
    """Short text fits in caption → 1 send_photo only, no separate text."""
    channel, bot = _make_channel(tmp_path)
    att = _attachment(tmp_path, "shot.png", "image", "image/png")
    receipt = await channel.deliver(
        _target(), DeliveryContent(text="look at this", attachments=(att,)),
    )
    bot.send_message.assert_not_awaited()
    bot.send_photo.assert_awaited_once()
    call_kwargs = bot.send_photo.await_args.kwargs
    assert call_kwargs["chat_id"] == 42
    assert call_kwargs["caption"] == "look at this"
    assert "photo" in call_kwargs  # file-like object
    assert receipt.external_message_id == "42:1"


@pytest.mark.asyncio
async def test_single_image_long_text_sends_preamble_then_photo(tmp_path):
    """Text > 1024 chars → text as standalone message, photo with no caption."""
    channel, bot = _make_channel(tmp_path)
    att = _attachment(tmp_path, "shot.png", "image", "image/png")
    long_text = "a" * 1500  # exceeds _CAPTION_MAX_CHARS=1024

    receipt = await channel.deliver(
        _target(), DeliveryContent(text=long_text, attachments=(att,)),
    )

    bot.send_message.assert_awaited_once()  # the preamble
    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs.get("caption") is None
    # Receipt tracks LAST surface (the photo, message_id=2).
    assert receipt.external_message_id == "42:2"


@pytest.mark.asyncio
async def test_multiple_attachments_only_first_carries_caption(tmp_path):
    """image + document, text fits → image carries caption, document doesn't."""
    channel, bot = _make_channel(tmp_path)
    img = _attachment(tmp_path, "a.png", "image", "image/png")
    doc = _attachment(tmp_path, "b.pdf", "document", "application/pdf")

    receipt = await channel.deliver(
        _target(), DeliveryContent(text="see attached", attachments=(img, doc)),
    )

    bot.send_message.assert_not_awaited()  # text rode along on img
    bot.send_photo.assert_awaited_once()
    bot.send_document.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs["caption"] == "see attached"
    assert bot.send_document.await_args.kwargs.get("caption") is None
    # Last surface = the document (message_id=2).
    assert receipt.external_message_id == "42:2"


@pytest.mark.asyncio
async def test_gif_mime_routes_to_send_animation(tmp_path):
    """image/gif → send_animation (auto-plays in Telegram) rather than send_photo."""
    channel, bot = _make_channel(tmp_path)
    gif = _attachment(tmp_path, "loop.gif", "image", "image/gif")
    await channel.deliver(_target(), DeliveryContent(text="", attachments=(gif,)))
    bot.send_animation.assert_awaited_once()
    bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_mp4_video_routes_to_send_animation_inline(tmp_path):
    """Short MP4s render as inline animations on Telegram — pick send_animation
    over send_video so the user gets auto-play instead of a click-to-play card."""
    channel, bot = _make_channel(tmp_path)
    mp4 = _attachment(tmp_path, "clip.mp4", "video", "video/mp4")
    await channel.deliver(_target(), DeliveryContent(text="", attachments=(mp4,)))
    bot.send_animation.assert_awaited_once()
    bot.send_video.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_kind_falls_back_to_send_document(tmp_path):
    """Unknown kind / mime → send_document (safe catch-all)."""
    channel, bot = _make_channel(tmp_path)
    weird = _attachment(tmp_path, "thing.bin", "octet-stream", "application/octet-stream")
    await channel.deliver(_target(), DeliveryContent(text="", attachments=(weird,)))
    bot.send_document.assert_awaited_once()


@pytest.mark.asyncio
async def test_one_attachment_failure_does_not_suppress_siblings(tmp_path):
    """If one attachment send raises, the remaining ones still go through.

    This is best-effort delivery — silently dropping all images because
    image #1 failed would be worse than a partial send. The exception is
    swallowed at the channel boundary (logged via logger.exception)."""
    channel, bot = _make_channel(tmp_path)
    img1 = _attachment(tmp_path, "ok.png", "image", "image/png")
    img2 = _attachment(tmp_path, "alsoOK.png", "image", "image/png")

    bot.send_photo.side_effect = [
        RuntimeError("network blip"),
        MagicMock(message_id=99),
    ]
    receipt = await channel.deliver(
        _target(), DeliveryContent(text="x", attachments=(img1, img2)),
    )

    assert bot.send_photo.await_count == 2
    # Receipt reflects the surviving send.
    assert receipt.external_message_id == "42:99"


@pytest.mark.asyncio
async def test_attachment_missing_storage_path_is_logged_and_skipped(tmp_path):
    """A malformed attachment dict (no storage_path) doesn't crash deliver."""
    channel, bot = _make_channel(tmp_path)
    bad = {"attachment_id": "x", "kind": "image", "mime_type": "image/png"}
    good = _attachment(tmp_path, "real.png", "image", "image/png")
    receipt = await channel.deliver(
        _target(), DeliveryContent(text="hi", attachments=(bad, good)),
    )
    # The good one still went through.
    bot.send_photo.assert_awaited_once()
    assert receipt.external_message_id is not None
