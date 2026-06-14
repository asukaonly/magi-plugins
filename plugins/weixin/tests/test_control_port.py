"""Weixin routes control commands through the host control port (not the dispatcher).

A control command (/新会话, /approve, /help …) is recognized + acked by the host's
unified ChannelControlPort, invoked BEFORE dispatch; the ack is surfaced to the
sender and the message never reaches the LLM dispatcher. A non-command falls
through to normal dispatch.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from magi_plugin_sdk.channels import (
    ChannelControlCommandResult,
    ChannelMessageDispatchOutcome,
    ChannelSessionMapping,
)

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


class _ControlPort:
    def __init__(self, channel: WeixinChannel, *, result) -> None:
        self.channel = channel
        self._result = result
        self.calls: list[str] = []

    async def handle_command(self, *, message, session_id, channel_type, external_chat_id, external_user_id):
        self.calls.append(message)
        if self.channel._stop_event is not None:
            self.channel._stop_event.set()
        return self._result


class _Dispatcher:
    def __init__(self, channel: WeixinChannel) -> None:
        self.channel = channel
        self.dispatched: list[str] = []

    async def dispatch_user_message(self, **kwargs):
        self.dispatched.append(str(kwargs["message"]))
        if self.channel._stop_event is not None:
            self.channel._stop_event.set()
        return ChannelMessageDispatchOutcome(
            success=True, user_id="local_user", session_id="chsess_test",
            turn_id="turn_x", message_id="m",
        )


class _Api:
    def __init__(self, message: dict) -> None:
        self._message = message
        self._served = False
        self.sent: list[tuple[str, str]] = []

    async def get_updates(self, *, get_updates_buf, timeout_ms):
        if self._served:
            return {"ret": 0, "get_updates_buf": "c", "msgs": []}
        self._served = True
        return {"ret": 0, "get_updates_buf": "c", "msgs": [self._message]}

    async def send_text_message(self, *, to_user_id, text, context_token, timeout_ms):
        self.sent.append((to_user_id, text))
        return "c1"


def _msg(text: str) -> dict:
    return {
        "message_id": "m1", "message_type": MESSAGE_TYPE_USER, "from_user_id": "user-1",
        "item_list": [{"type": MESSAGE_ITEM_TEXT, "text_item": {"text": text}}],
    }


def _channel(tmp_path: Path, api: _Api, *, result) -> WeixinChannel:
    WeixinStateStore(str(tmp_path)).save_credentials(
        WeixinCredentials(account_id="bot@im.bot", token="t")
    )
    ch = WeixinChannel(config=WeixinChannelConfig(
        state_dir=str(tmp_path), account_id="bot@im.bot", enable_typing_indicator=False,
    ))
    ch._credentials = WeixinCredentials(account_id="bot@im.bot", token="t")
    ch._api = api  # type: ignore[assignment]
    ch._stop_event = asyncio.Event()
    ch.bind_session_mapper(_Mapper())  # type: ignore[arg-type]
    ch.bind_message_dispatcher(_Dispatcher(ch))  # type: ignore[arg-type]
    ch.bind_control_port(_ControlPort(ch, result=result))
    return ch


@pytest.mark.asyncio
async def test_control_command_handled_by_port_acked_not_dispatched(tmp_path: Path) -> None:
    api = _Api(_msg("/新会话"))
    ch = _channel(tmp_path, api, result=ChannelControlCommandResult(ack="✨ 已重置", kind="session"))
    await ch._poll_loop()
    assert ch._control_port.calls == ["/新会话"]
    assert api.sent == [("user-1", "✨ 已重置")]
    assert ch._message_dispatcher.dispatched == []  # short-circuited, no LLM dispatch


@pytest.mark.asyncio
async def test_non_command_falls_through_to_dispatch(tmp_path: Path) -> None:
    api = _Api(_msg("今天天气怎么样"))
    ch = _channel(tmp_path, api, result=None)
    await ch._poll_loop()
    assert ch._control_port.calls == ["今天天气怎么样"]
    assert ch._message_dispatcher.dispatched == ["今天天气怎么样"]
    assert api.sent == []  # no command ack
