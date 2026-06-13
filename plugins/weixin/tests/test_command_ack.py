"""Weixin surfaces a host-handled command's ack to the sender.

A common channel command (/新会话, /reset, /approve …) is short-circuited by the
host's unified channel-command layer: it produces NO LLM turn (turn_id is None)
and returns its ack in error_message. Weixin must deliver that ack straight to
from_user_id — NOT via _send_text's session-mapping resolution, which a /新会话
reset has just deleted. Normal turns (turn_id set) reply via deliver(), so the
inbound path must NOT double-send for them.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from magi_plugin_sdk.channels import ChannelMessageDispatchOutcome, ChannelSessionMapping

from weixin.adapter import WeixinChannel, WeixinChannelConfig
from weixin.api import MESSAGE_ITEM_TEXT, MESSAGE_TYPE_USER
from weixin.state import WeixinCredentials, WeixinStateStore


class _Mapper:
    async def resolve_or_create(self, **kwargs):
        return ChannelSessionMapping(
            channel_type=kwargs["channel_type"],
            external_chat_id=kwargs["external_chat_id"],
            magi_session_id="chsess_test",
            magi_user_id="local_user",
        )


class _CommandDispatcher:
    """Simulates the host having short-circuited a command: success, no turn, an ack."""

    def __init__(self, channel: WeixinChannel, *, ack: str | None, turn_id) -> None:
        self._channel = channel
        self._ack = ack
        self._turn_id = turn_id

    async def dispatch_user_message(self, **kwargs):
        if self._channel._stop_event is not None:
            self._channel._stop_event.set()
        return ChannelMessageDispatchOutcome(
            success=True,
            user_id=str(kwargs["user_id"]),
            session_id=str(kwargs.get("session_id") or "chsess_test"),
            turn_id=self._turn_id,
            message_id=None,
            error_code=None,
            error_message=self._ack,
        )


class _UpdatesAndSendApi:
    """Combined fake: serves one inbound batch then drains; records text sends."""

    def __init__(self, message: dict) -> None:
        self._message = message
        self._served = False
        self.sent: list[tuple[str, str]] = []  # (to_user_id, text)

    async def get_updates(self, *, get_updates_buf: str, timeout_ms: int):
        _ = get_updates_buf, timeout_ms
        if self._served:
            return {"ret": 0, "get_updates_buf": "cursor", "msgs": []}
        self._served = True
        return {"ret": 0, "get_updates_buf": "cursor", "msgs": [self._message]}

    async def send_text_message(self, *, to_user_id: str, text: str, context_token, timeout_ms: int):
        _ = context_token, timeout_ms
        self.sent.append((to_user_id, text))
        return f"client-{len(self.sent)}"


def _text_message() -> dict:
    return {
        "message_id": "msg-cmd-1",
        "message_type": MESSAGE_TYPE_USER,
        "from_user_id": "user-1",
        "item_list": [{"type": MESSAGE_ITEM_TEXT, "text_item": {"text": "/新会话"}}],
    }


def _make_channel(tmp_path: Path, api: _UpdatesAndSendApi, dispatcher) -> WeixinChannel:
    WeixinStateStore(str(tmp_path)).save_credentials(
        WeixinCredentials(account_id="bot@im.bot", token="token")
    )
    channel = WeixinChannel(
        config=WeixinChannelConfig(
            state_dir=str(tmp_path), account_id="bot@im.bot", enable_typing_indicator=False,
        )
    )
    channel._credentials = WeixinCredentials(account_id="bot@im.bot", token="token")
    channel._api = api  # type: ignore[assignment]
    channel._stop_event = asyncio.Event()
    channel.bind_session_mapper(_Mapper())  # type: ignore[arg-type]
    channel.bind_message_dispatcher(dispatcher)  # type: ignore[arg-type]
    return channel


@pytest.mark.asyncio
async def test_command_ack_delivered_to_sender(tmp_path: Path) -> None:
    api = _UpdatesAndSendApi(_text_message())
    channel = _make_channel(
        tmp_path, api, None,  # type: ignore[arg-type]
    )
    channel.bind_message_dispatcher(  # type: ignore[arg-type]
        _CommandDispatcher(channel, ack="✨ 已重置,下一条消息开启全新对话。", turn_id=None)
    )
    await channel._poll_loop()
    assert api.sent == [("user-1", "✨ 已重置,下一条消息开启全新对话。")]


@pytest.mark.asyncio
async def test_normal_turn_does_not_send_inbound_ack(tmp_path: Path) -> None:
    """A normal turn (turn_id set) replies via deliver(); the inbound path stays silent."""
    api = _UpdatesAndSendApi(_text_message())
    channel = _make_channel(tmp_path, api, None)  # type: ignore[arg-type]
    channel.bind_message_dispatcher(  # type: ignore[arg-type]
        _CommandDispatcher(channel, ack=None, turn_id="turn_x")
    )
    await channel._poll_loop()
    assert api.sent == []
