from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from magi_plugin_sdk import ContributionType, PluginManifest
from magi_plugin_sdk.channels import ChannelMessageDispatchOutcome, ChannelSessionMapping, ChannelTarget, OutboundContent

from weixin.adapter import WeixinChannel, WeixinChannelConfig
from weixin.api import MESSAGE_ITEM_IMAGE, MESSAGE_ITEM_TEXT, MESSAGE_TYPE_USER, WeixinApiClient, WeixinApiError
from weixin.plugin import (
    CHANNEL_STATUS_RESOURCE_NAME,
    CLEAR_PROCESSED_MESSAGES_ACTION_ID,
    LOGOUT_ACTION_ID,
    RESET_CURSOR_ACTION_ID,
    VALIDATE_CREDENTIALS_ACTION_ID,
    WeixinPlugin,
)
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
        self.calls: list[dict] = []

    async def dispatch_user_message(self, **kwargs):
        self.messages.append(str(kwargs["message"]))
        self.calls.append(dict(kwargs))
        if self.channel._stop_event is not None:
            self.channel._stop_event.set()
        return ChannelMessageDispatchOutcome(
            success=self.success,
            user_id=str(kwargs["user_id"]),
            session_id=str(kwargs.get("session_id") or "chsess_test"),
            turn_id=str(kwargs.get("client_turn_id") or "turn_test"),
            message_id=f"msg_{len(self.calls)}",
            error_code=None if self.success else "dispatch_failed",
            error_message=None if self.success else "dispatch failed",
        )


class FakeUpdatesApi:
    def __init__(self, response: dict) -> None:
        self.response = response

    async def get_updates(self, *, get_updates_buf: str, timeout_ms: int):
        _ = get_updates_buf, timeout_ms
        return self.response


class FakeSendApi:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text_message(self, *, to_user_id: str, text: str, context_token: str | None, timeout_ms: int):
        _ = to_user_id, context_token, timeout_ms
        self.sent.append(text)
        return f"client-{len(self.sent)}"


class FakeAttachmentStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def store_attachment(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {
            "attachment_id": f"att-{len(self.calls)}",
            "kind": kwargs["kind"],
            "original_name": kwargs["original_name"],
            "mime_type": kwargs["mime_type"],
            "size_bytes": len(kwargs["content"]),
            "storage_path": f"/tmp/{kwargs['original_name']}",
            "sha256": "abc",
        }


def _message(message_id: str = "msg-1") -> dict:
    return {
        "message_id": message_id,
        "message_type": MESSAGE_TYPE_USER,
        "from_user_id": "user-1",
        "item_list": [{"type": MESSAGE_ITEM_TEXT, "text_item": {"text": "hello"}}],
    }


def _channel(
    tmp_path: Path,
    response: dict,
    *,
    dispatch_success: bool,
    attachment_store: FakeAttachmentStore | None = None,
) -> WeixinChannel:
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
    dispatcher = FakeDispatcher(success=dispatch_success, channel=channel)
    channel.bind_message_dispatcher(dispatcher)  # type: ignore[arg-type]
    if attachment_store is not None:
        channel.bind_attachment_store(attachment_store)  # type: ignore[arg-type]
    return channel


def _plugin(tmp_path: Path) -> WeixinPlugin:
    plugin = WeixinPlugin()
    plugin.configure(
        manifest=PluginManifest(
            plugin_id="weixin",
            name="Weixin",
            version="0.2.1",
            description="test",
            author="Magi Team",
            entry_module="plugin",
            entry_class="WeixinPlugin",
            contribution_types=[ContributionType.CHANNEL],
        ),
        settings={"state_dir": str(tmp_path), "account_id": "bot@im.bot"},
    )
    return plugin


@pytest.mark.asyncio
async def test_start_without_credentials_marks_channel_unconfigured(tmp_path: Path) -> None:
    channel = WeixinChannel(
        config=WeixinChannelConfig(
            state_dir=str(tmp_path),
            enable_typing_indicator=False,
        )
    )

    await channel.start()

    status = WeixinStateStore(str(tmp_path)).load_channel_status()
    assert status["state"] == "unconfigured"
    assert status["running"] is False
    assert status["configured"] is False
    assert status.get("last_error", "") == ""
    assert channel._poll_task is None


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
async def test_send_message_splits_long_text(tmp_path: Path) -> None:
    channel = WeixinChannel(
        config=WeixinChannelConfig(
            state_dir=str(tmp_path),
            account_id="bot@im.bot",
            max_message_length=5,
        )
    )
    api = FakeSendApi()
    channel._api = api  # type: ignore[assignment]
    channel._credentials = WeixinCredentials(account_id="bot@im.bot", token="token")

    await channel.send_message(ChannelTarget(channel_type="weixin", external_chat_id="user-1"), OutboundContent(text="hello world"))

    assert api.sent == ["hello", "world"]


@pytest.mark.asyncio
async def test_inbound_image_is_stored_as_attachment(tmp_path: Path, monkeypatch) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"image"

    def fake_download_media_bytes(*args, **kwargs):
        _ = args, kwargs
        return png

    monkeypatch.setattr("weixin.media._download_media_bytes", fake_download_media_bytes)
    attachment_store = FakeAttachmentStore()
    response = {
        "ret": 0,
        "get_updates_buf": "next-cursor",
        "msgs": [
            {
                "message_id": "img-1",
                "message_type": MESSAGE_TYPE_USER,
                "from_user_id": "user-1",
                "item_list": [
                    {
                        "type": MESSAGE_ITEM_IMAGE,
                        "image_item": {"media": {"full_url": "https://cdn.example/image.png"}},
                    }
                ],
            }
        ],
    }
    channel = _channel(tmp_path, response, dispatch_success=True, attachment_store=attachment_store)

    await channel._poll_loop()

    dispatcher = channel._message_dispatcher  # type: ignore[attr-defined]
    assert isinstance(dispatcher, FakeDispatcher)
    assert dispatcher.calls[0]["attachments"][0]["mime_type"] == "image/png"
    assert dispatcher.calls[0]["message"] == "[Image attached]"


@pytest.mark.asyncio
async def test_reply_reference_uses_saved_magi_message_id(tmp_path: Path) -> None:
    response = {
        "ret": 0,
        "get_updates_buf": "next-cursor",
        "msgs": [
            _message("m1"),
            {
                "message_id": "m2",
                "message_type": MESSAGE_TYPE_USER,
                "from_user_id": "user-1",
                "item_list": [
                    {
                        "type": MESSAGE_ITEM_TEXT,
                        "text_item": {"text": "reply"},
                        "ref_msg": {"message_item": {"msg_id": "m1", "type": MESSAGE_ITEM_TEXT, "text_item": {"text": "hello"}}},
                    }
                ],
            },
        ],
    }
    channel = _channel(tmp_path, response, dispatch_success=True)

    await channel._poll_loop()

    dispatcher = channel._message_dispatcher  # type: ignore[attr-defined]
    assert isinstance(dispatcher, FakeDispatcher)
    assert dispatcher.calls[1]["reply_to_message_id"] == "msg_1"


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
    plugin = _plugin(tmp_path)

    result = await plugin.start_settings_action(VALIDATE_CREDENTIALS_ACTION_ID, session_id="validate-1")

    assert result.status == "succeeded"
    assert result.data["account_id"] == "bot@im.bot"


def test_channel_status_resource_reads_state(tmp_path: Path) -> None:
    store = WeixinStateStore(str(tmp_path))
    store.update_channel_status(state="running", running=True, account_id="bot@im.bot")
    plugin = _plugin(tmp_path)

    status = plugin.read_settings_resource(CHANNEL_STATUS_RESOURCE_NAME)

    assert status["state"] == "running"
    assert status["running"] is True
    assert status["account_id"] == "bot@im.bot"


@pytest.mark.asyncio
async def test_maintenance_actions_reset_state_and_logout(tmp_path: Path) -> None:
    store = WeixinStateStore(str(tmp_path))
    store.save_credentials(WeixinCredentials(account_id="bot@im.bot", token="token"))
    store.save_sync_buf("bot@im.bot", "cursor")
    store.save_processed_message_ids("bot@im.bot", {"msg-1"})
    plugin = _plugin(tmp_path)

    reset = await plugin.start_settings_action(RESET_CURSOR_ACTION_ID, session_id="reset-1")
    clear = await plugin.start_settings_action(CLEAR_PROCESSED_MESSAGES_ACTION_ID, session_id="clear-1")

    assert reset.status == "succeeded"
    assert reset.data["refresh_channels"] is True
    assert store.load_sync_buf("bot@im.bot") == ""
    assert clear.status == "succeeded"
    assert store.load_processed_message_ids("bot@im.bot") == set()

    logout = await plugin.start_settings_action(LOGOUT_ACTION_ID, session_id="logout-1")

    assert logout.status == "succeeded"
    assert store.load_credentials(account_id="bot@im.bot") is None
    assert logout.settings_updates["account_id"] == ""