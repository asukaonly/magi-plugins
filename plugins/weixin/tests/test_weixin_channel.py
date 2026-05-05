from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from magi_plugin_sdk import ContributionType, PluginManifest
from magi_plugin_sdk.channels import ChannelMessageDispatchOutcome, ChannelSessionMapping

from weixin.adapter import WeixinChannel, WeixinChannelConfig
from weixin.api import MESSAGE_ITEM_TEXT, MESSAGE_TYPE_USER, WeixinApiClient, WeixinApiError
from weixin.plugin import CHANNEL_STATUS_RESOURCE_NAME, VALIDATE_CREDENTIALS_ACTION_ID, WeixinPlugin
from weixin.state import WeixinCredentials, WeixinStateStore


class FakeMapper:
    async def resolve_or_create(self, **kwargs):
        return ChannelSessionMapping(
            channel_type=kwargs["channel_type"],
            external_chat_id=kwargs["external_chat_id"],
            magi_session_id="chsess_test",
            magi_user_id="channel_weixin_user-1",
        )


class FakeDispatcher:
    def __init__(self, *, success: bool, channel: WeixinChannel) -> None:
        self.success = success
        self.channel = channel
        self.messages: list[str] = []

    async def dispatch_user_message(self, **kwargs):
        self.messages.append(str(kwargs["message"]))
        if self.channel._stop_event is not None:
            self.channel._stop_event.set()
        return ChannelMessageDispatchOutcome(
            success=self.success,
            user_id=str(kwargs["user_id"]),
            session_id=str(kwargs.get("session_id") or "chsess_test"),
            error_code=None if self.success else "dispatch_failed",
            error_message=None if self.success else "dispatch failed",
        )


class FakeUpdatesApi:
    def __init__(self, response: dict) -> None:
        self.response = response

    async def get_updates(self, *, get_updates_buf: str, timeout_ms: int):
        _ = get_updates_buf, timeout_ms
        return self.response


def _message(message_id: str = "msg-1") -> dict:
    return {
        "message_id": message_id,
        "message_type": MESSAGE_TYPE_USER,
        "from_user_id": "user-1",
        "item_list": [{"type": MESSAGE_ITEM_TEXT, "text_item": {"text": "hello"}}],
    }


def _channel(tmp_path: Path, response: dict, *, dispatch_success: bool) -> WeixinChannel:
    store = WeixinStateStore(str(tmp_path))
    store.save_credentials(WeixinCredentials(account_id="bot@im.bot", token="token"))
    channel = WeixinChannel(
        config=WeixinChannelConfig(
            state_dir=str(tmp_path),
            account_id="bot@im.bot",
            enable_typing_indicator=False,
        )
    )
    channel._credentials = WeixinCredentials(account_id="bot@im.bot", token="token")
    channel._api = FakeUpdatesApi(response)  # type: ignore[assignment]
    channel._stop_event = asyncio.Event()
    channel.bind_session_mapper(FakeMapper())  # type: ignore[arg-type]
    channel.bind_message_dispatcher(FakeDispatcher(success=dispatch_success, channel=channel))  # type: ignore[arg-type]
    return channel


@pytest.mark.asyncio
async def test_send_text_message_raises_on_protocol_error(monkeypatch) -> None:
    client = WeixinApiClient(token="token")

    async def fake_request(self, *args, **kwargs):
        _ = self, args, kwargs
        return {"ret": 1, "errmsg": "bad token"}

    monkeypatch.setattr(WeixinApiClient, "_request_json", fake_request)

    with pytest.raises(WeixinApiError, match="bad token"):
        await client.send_text_message(to_user_id="user-1", text="hello", context_token=None)


@pytest.mark.asyncio
async def test_getupdates_cursor_waits_for_successful_dispatch(tmp_path: Path) -> None:
    response = {"ret": 0, "get_updates_buf": "next-cursor", "msgs": [_message()]}
    failed_channel = _channel(tmp_path, response, dispatch_success=False)

    await failed_channel._poll_loop()

    store = WeixinStateStore(str(tmp_path))
    assert store.load_sync_buf("bot@im.bot") == ""

    succeeded_channel = _channel(tmp_path, response, dispatch_success=True)
    await succeeded_channel._poll_loop()

    assert store.load_sync_buf("bot@im.bot") == "next-cursor"
    assert "msg-1" in store.load_processed_message_ids("bot@im.bot")


@pytest.mark.asyncio
async def test_validate_credentials_action_uses_saved_credentials(tmp_path: Path, monkeypatch) -> None:
    store = WeixinStateStore(str(tmp_path))
    store.save_credentials(WeixinCredentials(account_id="bot@im.bot", token="token"))

    class FakeClient:
        def __init__(self, **kwargs):
            self.base_url = kwargs["base_url"]

        async def get_updates(self, *, get_updates_buf: str, timeout_ms: int):
            _ = get_updates_buf, timeout_ms
            return {"ret": 0, "msgs": [], "get_updates_buf": "same"}

    monkeypatch.setattr("weixin.plugin.WeixinApiClient", FakeClient)
    plugin = WeixinPlugin()
    plugin.configure(
        manifest=PluginManifest(
            plugin_id="weixin",
            name="Weixin",
            version="0.1.5",
            description="test",
            author="Magi Team",
            entry_module="plugin",
            entry_class="WeixinPlugin",
            contribution_types=[ContributionType.CHANNEL],
        ),
        settings={"state_dir": str(tmp_path), "account_id": "bot@im.bot"},
    )

    result = await plugin.start_settings_action(VALIDATE_CREDENTIALS_ACTION_ID, session_id="validate-1")

    assert result.status == "succeeded"
    assert result.data["account_id"] == "bot@im.bot"


def test_channel_status_resource_reads_state(tmp_path: Path) -> None:
    store = WeixinStateStore(str(tmp_path))
    store.update_channel_status(state="running", running=True, account_id="bot@im.bot")
    plugin = WeixinPlugin()
    plugin.configure(
        manifest=PluginManifest(
            plugin_id="weixin",
            name="Weixin",
            version="0.1.5",
            description="test",
            author="Magi Team",
            entry_module="plugin",
            entry_class="WeixinPlugin",
            contribution_types=[ContributionType.CHANNEL],
        ),
        settings={"state_dir": str(tmp_path), "account_id": "bot@im.bot"},
    )

    status = plugin.read_settings_resource(CHANNEL_STATUS_RESOURCE_NAME)

    assert status["state"] == "running"
    assert status["running"] is True
    assert status["account_id"] == "bot@im.bot"