"""Weixin ``deliver_control_request`` — CF-10.

WeChat lacks an inline-button primitive, so the control prompt is
rendered as a text message with explicit ``/approve <short_id>`` /
``/deny <short_id>`` instructions. The user types the slash command
as a regular reply; CF-6's host-side parser resolves the broker.

Pins:
* ``supports_control_requests`` is True (host fanout includes us).
* ``deliver_control_request`` sends a single text message via
  ``_send_text`` containing the tool name, the preview, and the
  two slash-command instructions verbatim with the short_id.
* Exception in _send_text is swallowed (host fanout's per-channel
  isolation depends on this).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from magi_plugin_sdk import ControlRequest
from magi_plugin_sdk.channels import ChannelTarget

from weixin.adapter import WeixinChannel, WeixinChannelConfig
from weixin.state import WeixinCredentials


class _FakeApi:
    """Stub that records sent text (mirrors FakeSendApi shape from
    test_weixin_channel)."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text_message(
        self, *, to_user_id: str, text: str,
        context_token: str | None, timeout_ms: int,
    ):
        _ = to_user_id, context_token, timeout_ms
        self.sent.append(text)
        return f"client-{len(self.sent)}"


def _make_channel(tmp_path: Path) -> WeixinChannel:
    config = WeixinChannelConfig(
        state_dir=str(tmp_path),
        account_id="bot@im.bot",
        max_message_length=2000,
    )
    channel = WeixinChannel(config=config)
    channel._api = _FakeApi()  # type: ignore[assignment]
    channel._credentials = WeixinCredentials(
        account_id="bot@im.bot", token="token",
    )
    return channel


def _make_target(*, session_id: str = "s-1") -> ChannelTarget:
    return ChannelTarget(
        channel_type="weixin",
        external_chat_id="user-1",
        magi_session_id=session_id,
        magi_user_id="local_user",
    )


def _make_request(*, short_id: str = "abc123") -> ControlRequest:
    return ControlRequest(
        request_id=f"01HFTGSM7Z8X9YQK4PVAN3{short_id.upper()}",
        short_id=short_id,
        kind="permission",
        tool_name="image_gen",
        preview="Generate a cat",
    )


def test_supports_control_requests_is_true() -> None:
    assert WeixinChannel.supports_control_requests is True


@pytest.mark.asyncio
async def test_deliver_control_request_sends_instructions_text(
    tmp_path: Path,
) -> None:
    channel = _make_channel(tmp_path)
    api: _FakeApi = channel._api  # type: ignore[assignment]

    await channel.deliver_control_request(
        _make_target(), _make_request(short_id="abc123"),
    )

    assert len(api.sent) == 1
    msg = api.sent[0]
    # Tool name and preview surfaced.
    assert "image_gen" in msg
    assert "Generate a cat" in msg
    # Both slash commands with the short_id present and parseable.
    assert "/approve abc123" in msg
    assert "/deny abc123" in msg


@pytest.mark.asyncio
async def test_deliver_control_request_swallows_exceptions(
    tmp_path: Path,
) -> None:
    """If _send_text raises (Weixin API down), the host fanout's
    per-channel isolation requires us to NOT propagate — other
    channels' control requests must continue."""
    channel = _make_channel(tmp_path)
    channel._send_text = AsyncMock(side_effect=RuntimeError("api down"))  # type: ignore[method-assign]

    # Must not raise.
    await channel.deliver_control_request(_make_target(), _make_request())
