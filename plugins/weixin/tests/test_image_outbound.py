"""End-to-end pin of the Weixin image outbound pipeline.

Verifies the 3-step iLink dance happens in the right order with the
right arguments when ``WeixinChannel.deliver`` gets a DeliveryContent
carrying an image attachment:

  1. ``get_upload_url`` is called with plaintext sizes, plaintext MD5,
     ciphertext size, and the base64 AES key.
  2. ``upload_to_cdn`` is called (PUT) with the AES-128-ECB ciphertext.
  3. ``send_image_message`` is called with the upload_param echoed back
     as ``encrypt_query_param`` and the same AES key, plus thumb refs.

The receipt's ``external_message_id`` tracks the image's client_id
(the LAST sent surface) so retract operates on the most recent message.
"""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from magi_plugin_sdk.channels import ChannelTarget
from magi_plugin_sdk.delivery import DeliveryContent

from weixin.adapter import WeixinChannel, WeixinChannelConfig
from weixin.media_upload import aes_128_ecb_decrypt
from weixin.state import WeixinCredentials


def _png_bytes(w: int = 200, h: int = 100) -> bytes:
    from PIL import Image
    img = Image.new("RGB", (w, h), (10, 200, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_channel(tmp_path: Path) -> WeixinChannel:
    channel = WeixinChannel(config=WeixinChannelConfig(
        state_dir=str(tmp_path), account_id="bot@im.bot",
    ))
    channel._credentials = WeixinCredentials(account_id="bot@im.bot", token="t")

    # Realistic iLink response shape per the openclaw-weixin TS types.
    api = AsyncMock()
    api.get_upload_url = AsyncMock(return_value={
        "ret": 0,
        "upload_param": "ENCRYPTED-MAIN-PARAM",
        "upload_full_url": "https://cdn.example/main",
        "thumb_upload_param": "ENCRYPTED-THUMB-PARAM",
        "thumb_upload_full_url": "https://cdn.example/thumb",
    })
    api.upload_to_cdn = AsyncMock(return_value=None)
    api.send_image_message = AsyncMock(return_value="magi-weixin-test-cid")
    api.send_text_message = AsyncMock(return_value="magi-weixin-text-cid")
    channel._api = api  # type: ignore[assignment]
    return channel


def _img_attachment(tmp_path: Path, name: str = "shot.png") -> dict:
    payload = _png_bytes()
    p = tmp_path / name
    p.write_bytes(payload)
    return {
        "attachment_id": f"att-{name}",
        "kind": "image",
        "original_name": name,
        "mime_type": "image/png",
        "size_bytes": len(payload),
        "storage_path": str(p),
        "sha256": "fake",
    }


def _target(tmp_path: Path) -> ChannelTarget:
    # Pre-fill external_chat_id so the resolver short-circuits and we
    # don't need a session_mapper fake.
    return ChannelTarget(
        channel_type="weixin",
        external_chat_id="o9cq_user@im.wechat",
        magi_session_id="chsess_wx",
        magi_user_id="channel_weixin_o9cq",
    )


@pytest.mark.asyncio
async def test_deliver_text_plus_image_calls_3_step_pipeline(tmp_path):
    channel = _make_channel(tmp_path)
    att = _img_attachment(tmp_path)
    receipt = await channel.deliver(
        _target(tmp_path),
        DeliveryContent(text="here you go", attachments=(att,)),
    )

    # Step 0: text fired first (existing path, separate API).
    channel._api.send_text_message.assert_awaited_once()

    # Step 1: getuploadurl with correct sizes + MD5 + base64 key.
    channel._api.get_upload_url.assert_awaited_once()
    upload_kwargs = channel._api.get_upload_url.await_args.kwargs
    assert upload_kwargs["to_user_id"] == "o9cq_user@im.wechat"
    assert upload_kwargs["media_type"] == 2  # MESSAGE_ITEM_IMAGE
    assert upload_kwargs["raw_size"] == att["size_bytes"]
    # AES-128 ciphertext is plaintext-padded to a 16-byte multiple.
    assert upload_kwargs["cipher_size"] % 16 == 0
    assert upload_kwargs["cipher_size"] >= upload_kwargs["raw_size"]
    # Base64 of a 16-byte AES key → 24 chars including padding "=".
    assert len(upload_kwargs["aes_key_b64"]) == 24

    # Step 2: PUT both ciphertexts. Order: main first, then thumb.
    assert channel._api.upload_to_cdn.await_count == 2
    main_call, thumb_call = channel._api.upload_to_cdn.await_args_list
    assert main_call.kwargs["upload_full_url"] == "https://cdn.example/main"
    assert thumb_call.kwargs["upload_full_url"] == "https://cdn.example/thumb"

    # The encrypted bytes should round-trip back to plaintext under the
    # same AES key, proving we PUT what the recipient will actually be
    # able to decrypt.
    import base64
    key = base64.b64decode(upload_kwargs["aes_key_b64"])
    main_cipher = main_call.kwargs["encrypted_bytes"]
    recovered = aes_128_ecb_decrypt(main_cipher, key)
    # Recovered bytes should match the original PNG we wrote to disk.
    assert recovered == Path(att["storage_path"]).read_bytes()

    # Step 3: sendmessage with the params echoed back + same key.
    channel._api.send_image_message.assert_awaited_once()
    send_kwargs = channel._api.send_image_message.await_args.kwargs
    assert send_kwargs["to_user_id"] == "o9cq_user@im.wechat"
    assert send_kwargs["image_param"] == "ENCRYPTED-MAIN-PARAM"
    assert send_kwargs["thumb_param"] == "ENCRYPTED-THUMB-PARAM"
    assert send_kwargs["image_aes_key_b64"] == upload_kwargs["aes_key_b64"]
    assert send_kwargs["thumb_aes_key_b64"] == upload_kwargs["aes_key_b64"]
    assert send_kwargs["image_size"] == att["size_bytes"]
    assert send_kwargs["thumb_width"] > 0
    assert send_kwargs["thumb_height"] > 0

    # Receipt tracks the LAST surface (the image, not the text).
    assert receipt.external_message_id == "magi-weixin-test-cid"
    assert receipt.channel_id == "weixin"


@pytest.mark.asyncio
async def test_deliver_image_only_no_text_skips_send_text(tmp_path):
    """Empty text → no send_text_message call, but image pipeline still
    runs. Receipt is the image's client_id."""
    channel = _make_channel(tmp_path)
    att = _img_attachment(tmp_path)
    receipt = await channel.deliver(
        _target(tmp_path),
        DeliveryContent(text="", attachments=(att,)),
    )
    channel._api.send_text_message.assert_not_awaited()
    channel._api.send_image_message.assert_awaited_once()
    assert receipt.external_message_id == "magi-weixin-test-cid"


@pytest.mark.asyncio
async def test_non_image_attachment_is_skipped_with_warning(tmp_path, caplog):
    """document / video / etc. aren't wired into the iLink pipeline yet
    (each needs its own item type metadata). They MUST be skipped with
    a clear log, not silently dropped or sent as text."""
    import logging

    channel = _make_channel(tmp_path)
    pdf_path = tmp_path / "spec.pdf"
    pdf_path.write_bytes(b"%PDF-fake")
    doc_att = {
        "attachment_id": "att-pdf",
        "kind": "document",
        "original_name": "spec.pdf",
        "mime_type": "application/pdf",
        "size_bytes": pdf_path.stat().st_size,
        "storage_path": str(pdf_path),
        "sha256": "fake",
    }

    with caplog.at_level(logging.WARNING, logger="magi_plugin_weixin.adapter"):
        receipt = await channel.deliver(
            _target(tmp_path),
            DeliveryContent(text="see attached", attachments=(doc_att,)),
        )

    channel._api.send_text_message.assert_awaited_once()  # text still goes
    channel._api.get_upload_url.assert_not_awaited()      # no image flow
    channel._api.send_image_message.assert_not_awaited()
    # Receipt falls back to the text client_id.
    assert receipt.external_message_id == "magi-weixin-text-cid"
    # The skip is logged at WARNING so ops can spot it.
    assert any("Weixin attachment skipped" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_failed_attachment_does_not_abort_siblings(tmp_path):
    """If one image fails mid-pipeline (e.g. CDN PUT 503), the next image
    still goes through. Best-effort delivery."""
    channel = _make_channel(tmp_path)
    a = _img_attachment(tmp_path, "a.png")
    b = _img_attachment(tmp_path, "b.png")

    # First CDN upload raises; second succeeds.
    channel._api.upload_to_cdn = AsyncMock(side_effect=[
        RuntimeError("cdn 503"),  # main of A fails
        None,  # main of B
        None,  # thumb of B
    ])
    channel._api.send_image_message = AsyncMock(side_effect=[
        # First attachment's send never gets called because PUT failed,
        # but the AsyncMock has to be ready for the second.
        "magi-weixin-second",
    ])

    receipt = await channel.deliver(
        _target(tmp_path),
        DeliveryContent(text="", attachments=(a, b)),
    )

    assert channel._api.send_image_message.await_count == 1
    # Receipt tracks the surviving send.
    assert receipt.external_message_id == "magi-weixin-second"


@pytest.mark.asyncio
async def test_text_only_path_unchanged(tmp_path):
    """No attachments → only text goes out. Pins the no-regression on
    the existing weixin behavior."""
    channel = _make_channel(tmp_path)
    receipt = await channel.deliver(
        _target(tmp_path),
        DeliveryContent(text="hello"),
    )
    channel._api.send_text_message.assert_awaited_once()
    channel._api.get_upload_url.assert_not_awaited()
    channel._api.upload_to_cdn.assert_not_awaited()
    channel._api.send_image_message.assert_not_awaited()
    assert receipt.external_message_id == "magi-weixin-text-cid"
