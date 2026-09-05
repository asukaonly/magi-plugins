"""Weixin provider-time admission and local inbound clear behavior."""

from __future__ import annotations

from sdk_test_support import credentials_for_path

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from magi_plugin_sdk.channels import (
    ChannelInboundClearRequest,
    ChannelInboundClearStrategy,
    ChannelInboundContext,
    ChannelInboundRejectedError,
    ChannelInboundRejectionReason,
    ChannelMessageDispatchOutcome,
    ChannelProviderTimeEvidence,
    ChannelSessionMapping,
)
from magi_plugin_sdk.fs import UnsafeManagedPathError

from weixin.adapter import WeixinChannel, WeixinChannelConfig
from weixin.api import MESSAGE_ITEM_IMAGE, MESSAGE_ITEM_TEXT, MESSAGE_TYPE_USER
from weixin.state import WeixinCredentials, WeixinStateStore


PROVIDER_TIME_MS = 1_754_017_445_000
_MISSING = object()


def _message(
    text: str = "hello",
    *,
    message_id: str = "message-1",
    create_time_ms: object = PROVIDER_TIME_MS,
    context_token: str = "",
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": message_id,
        "message_type": MESSAGE_TYPE_USER,
        "from_user_id": "user-1",
        "item_list": [
            {"type": MESSAGE_ITEM_TEXT, "text_item": {"text": text}}
        ],
    }
    if create_time_ms is not _MISSING:
        message["create_time_ms"] = create_time_ms
    if context_token:
        message["context_token"] = context_token
    return message


def _context(
    evidence: ChannelProviderTimeEvidence,
    *,
    generation: int = 0,
) -> ChannelInboundContext:
    return ChannelInboundContext(
        channel_type="weixin",
        stream_id="user-1",
        admission_evidence=evidence,
        clear_generation=generation,
    )


def _make_indirect_entry(
    path: Path,
    external_target: Path,
    entry_kind: str,
) -> None:
    if entry_kind == "symlink":
        path.symlink_to(external_target)
        return
    if entry_kind == "hardlink":
        os.link(external_target, path)
        return
    if entry_kind == "fifo":
        if os.name != "posix" or not hasattr(os, "mkfifo"):
            pytest.skip("POSIX FIFOs are unavailable")
        os.mkfifo(path)
        return
    raise AssertionError(f"unsupported entry kind: {entry_kind}")


def _wired_channel(
    tmp_path: Path,
) -> tuple[WeixinChannel, MagicMock, MagicMock, MagicMock]:
    credentials = WeixinCredentials(account_id="bot@im.bot", token="token")
    store = WeixinStateStore(str(tmp_path), credentials=credentials_for_path(str(tmp_path)))
    store.save_credentials(credentials)
    channel = WeixinChannel(state_store=WeixinStateStore(str(tmp_path), credentials=credentials_for_path(str(tmp_path))),
        config=WeixinChannelConfig(

            account_id=credentials.account_id,
            enable_typing_indicator=False,
        )
    )
    channel._credentials = credentials
    channel._stop_event = asyncio.Event()

    dispatcher = MagicMock()

    async def capture(**kwargs):
        return _context(
            kwargs["evidence"],
            generation=channel._applied_inbound_clear_generation,
        )

    dispatcher.capture_inbound_context = AsyncMock(side_effect=capture)
    dispatcher.dispatch_user_message = AsyncMock(
        return_value=ChannelMessageDispatchOutcome(
            success=True,
            user_id="local-user",
            session_id="session-1",
            turn_id="turn-1",
            message_id="magi-message-1",
        )
    )

    mapper = MagicMock()
    mapper.resolve_or_create = AsyncMock(
        return_value=ChannelSessionMapping(
            channel_type="weixin",
            external_chat_id="user-1",
            magi_session_id="session-1",
            magi_user_id="local-user",
        )
    )

    control_port = MagicMock()
    control_port.handle_command = AsyncMock(return_value=None)
    channel.bind_message_dispatcher(dispatcher)
    channel.bind_session_mapper(mapper)
    channel.bind_control_port(control_port)
    return channel, dispatcher, mapper, control_port


class _OneShotUpdatesApi:
    def __init__(self, channel: WeixinChannel, response: dict[str, object]) -> None:
        self._channel = channel
        self._response = response
        self.calls = 0

    async def get_updates(self, *, get_updates_buf: str, timeout_ms: int):
        _ = get_updates_buf, timeout_ms
        self.calls += 1
        if self._channel._stop_event is not None:
            self._channel._stop_event.set()
        return self._response


def test_weixin_declares_provider_time_clear_strategy() -> None:
    assert (
        WeixinChannel.inbound_clear_strategy
        is ChannelInboundClearStrategy.PROVIDER_TIME
    )


@pytest.mark.asyncio
async def test_provider_context_is_captured_first_and_reused_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel, dispatcher, mapper, control_port = _wired_channel(tmp_path)
    inbound_context = _context(
        ChannelProviderTimeEvidence(provider_occurred_at_ms=PROVIDER_TIME_MS)
    )
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

    async def typing(_target, **_kwargs):
        order.append("typing")

    async def dispatch(**_kwargs):
        order.append("dispatch")
        return dispatcher.dispatch_user_message.return_value

    def save_context_tokens(_account_id, _tokens):
        order.append("context-token")

    dispatcher.capture_inbound_context.side_effect = capture
    mapper.resolve_or_create.side_effect = resolve
    control_port.handle_command.side_effect = control
    dispatcher.dispatch_user_message.side_effect = dispatch
    monkeypatch.setattr(channel, "send_typing_indicator", typing)
    monkeypatch.setattr(channel._state, "save_context_tokens", save_context_tokens)

    processed = await channel._process_inbound_message(
        _message(context_token="provider-context-token")
    )

    assert processed is True
    assert order == [
        "capture",
        "context-token",
        "mapping",
        "control",
        "typing",
        "dispatch",
    ]
    capture_kwargs = dispatcher.capture_inbound_context.await_args.kwargs
    assert capture_kwargs == {
        "channel_type": "weixin",
        "stream_id": "user-1",
        "evidence": ChannelProviderTimeEvidence(
            provider_occurred_at_ms=PROVIDER_TIME_MS
        ),
    }
    assert mapper.resolve_or_create.await_args.kwargs["inbound_context"] is inbound_context
    assert control_port.handle_command.await_args.kwargs["inbound_context"] is inbound_context
    assert dispatcher.dispatch_user_message.await_args.kwargs["inbound_context"] is inbound_context


@pytest.mark.parametrize(
    "create_time_ms",
    [_MISSING, None, 0, -1, True, "1754017445000", 1.5],
)
@pytest.mark.asyncio
async def test_invalid_provider_time_is_terminal_and_advances_local_cursor(
    tmp_path: Path,
    create_time_ms: object,
) -> None:
    channel, dispatcher, mapper, control_port = _wired_channel(tmp_path)
    message = _message(create_time_ms=create_time_ms)
    channel._api = _OneShotUpdatesApi(
        channel,
        {
            "ret": 0,
            "get_updates_buf": "next-cursor",
            "msgs": [message],
        },
    )

    await channel._poll_loop()

    store = WeixinStateStore(str(tmp_path), credentials=credentials_for_path(str(tmp_path)))
    assert store.load_sync_buf("bot@im.bot") == "next-cursor"
    assert "message-1" in store.load_processed_message_ids("bot@im.bot")
    dispatcher.capture_inbound_context.assert_not_awaited()
    mapper.resolve_or_create.assert_not_awaited()
    control_port.handle_command.assert_not_awaited()
    dispatcher.dispatch_user_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_provider_message_rejected_by_host_is_terminal(
    tmp_path: Path,
) -> None:
    channel, dispatcher, mapper, control_port = _wired_channel(tmp_path)
    old_time_ms = PROVIDER_TIME_MS - 60_000
    dispatcher.capture_inbound_context.side_effect = ChannelInboundRejectedError(
        ChannelInboundRejectionReason.CLEARED_MESSAGE,
        "provider event predates clear",
    )
    channel._api = _OneShotUpdatesApi(
        channel,
        {
            "ret": 0,
            "get_updates_buf": "after-old-message",
            "msgs": [_message(create_time_ms=old_time_ms)],
        },
    )

    await channel._poll_loop()

    evidence = dispatcher.capture_inbound_context.await_args.kwargs["evidence"]
    assert evidence == ChannelProviderTimeEvidence(
        provider_occurred_at_ms=old_time_ms
    )
    store = WeixinStateStore(str(tmp_path), credentials=credentials_for_path(str(tmp_path)))
    assert store.load_sync_buf("bot@im.bot") == "after-old-message"
    assert "message-1" in store.load_processed_message_ids("bot@im.bot")
    mapper.resolve_or_create.assert_not_awaited()
    control_port.handle_command.assert_not_awaited()
    dispatcher.dispatch_user_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_host_rejection_is_terminal_and_advances_local_cursor(
    tmp_path: Path,
) -> None:
    channel, dispatcher, _, _ = _wired_channel(tmp_path)
    dispatcher.dispatch_user_message.side_effect = ChannelInboundRejectedError(
        ChannelInboundRejectionReason.CLEARED_MESSAGE,
        "event crossed clear during processing",
    )
    channel._api = _OneShotUpdatesApi(
        channel,
        {
            "ret": 0,
            "get_updates_buf": "after-late-rejection",
            "msgs": [_message()],
        },
    )

    await channel._poll_loop()

    store = WeixinStateStore(str(tmp_path), credentials=credentials_for_path(str(tmp_path)))
    assert store.load_sync_buf("bot@im.bot") == "after-late-rejection"
    assert "message-1" in store.load_processed_message_ids("bot@im.bot")
    dispatcher.capture_inbound_context.assert_awaited_once()
    dispatcher.dispatch_user_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_attachment_store_receives_captured_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel, dispatcher, _, _ = _wired_channel(tmp_path)
    inbound_context = _context(
        ChannelProviderTimeEvidence(provider_occurred_at_ms=PROVIDER_TIME_MS)
    )
    dispatcher.capture_inbound_context.return_value = inbound_context
    dispatcher.capture_inbound_context.side_effect = None
    attachment_store = MagicMock()
    attachment_store.store_attachment = AsyncMock(
        return_value={
            "attachment_id": "attachment-1",
            "kind": "image",
            "original_name": "image.png",
            "mime_type": "image/png",
            "size_bytes": 12,
        }
    )
    channel.bind_attachment_store(attachment_store)
    monkeypatch.setattr(
        "weixin.media._download_media_bytes",
        lambda *_args, **_kwargs: b"\x89PNG\r\n\x1a\nimage",
    )
    message = _message(text="")
    message["item_list"] = [
        {
            "type": MESSAGE_ITEM_IMAGE,
            "image_item": {"media": {"full_url": "https://example.test/image"}},
        }
    ]

    processed = await channel._process_inbound_message(message)

    assert processed is True
    assert (
        attachment_store.store_attachment.await_args.kwargs["inbound_context"]
        is inbound_context
    )
    assert (
        dispatcher.dispatch_user_message.await_args.kwargs["inbound_context"]
        is inbound_context
    )


@pytest.mark.asyncio
async def test_clear_erases_inbound_content_but_preserves_account_and_cursor(
    tmp_path: Path,
) -> None:
    store = WeixinStateStore(str(tmp_path), credentials=credentials_for_path(str(tmp_path)))
    credentials = WeixinCredentials(account_id="bot@im.bot", token="secret-token")
    store.save_credentials(credentials)
    store.save_sync_buf(credentials.account_id, "provider-cursor")
    store.save_context_tokens(credentials.account_id, {"user-1": "context-token"})
    store.save_processed_message_ids(credentials.account_id, {"old-message"})
    store.save_message_id_mapping(
        credentials.account_id,
        "external-message",
        "magi-message",
    )
    store.update_channel_status(
        state="running",
        running=True,
        configured=True,
        account_id=credentials.account_id,
        base_url="https://provider.test/api?private=secret",
        last_poll_at_ms=PROVIDER_TIME_MS,
        last_start_at_ms=PROVIDER_TIME_MS,
        last_stop_at_ms=PROVIDER_TIME_MS,
        last_inbound_at_ms=PROVIDER_TIME_MS,
        last_outbound_at_ms=PROVIDER_TIME_MS,
        last_inbound_chat_id="user-1",
        last_outbound_chat_id="user-1",
        last_outbound_client_id="client-1",
        last_outbound_part_count=3,
        last_error="message body",
        last_error_at_ms=PROVIDER_TIME_MS,
        inbound_body="private inbound body",
        outbound_body="private outbound body",
        unknown_secret="must not survive",
    )
    preserved_settings = tmp_path / "operator-settings.json"
    preserved_settings.write_text('{"enabled":true}', encoding="utf-8")

    channel = WeixinChannel(state_store=WeixinStateStore(str(tmp_path), credentials=credentials_for_path(str(tmp_path))),
        config=WeixinChannelConfig(

            account_id=credentials.account_id,
        )
    )
    channel._credentials = credentials
    channel._context_tokens = {"user-1": "context-token"}
    channel._processed_message_ids = {"old-message"}
    channel._typing_cache["user-1"] = SimpleNamespace(
        ticket="typing-ticket",
        next_refresh_at_ms=PROVIDER_TIME_MS,
    )
    api = MagicMock()
    api.get_updates = AsyncMock()
    api.get_config = AsyncMock()
    api.send_typing = AsyncMock()
    channel._api = api

    async with channel.inbound_clear_boundary(
        ChannelInboundClearRequest(
            channel_type="weixin",
            clear_generation=3,
        )
    ):
        assert channel._inbound_clear_active is True
        assert channel._context_tokens == {}
        assert channel._processed_message_ids == set()
        assert channel._typing_cache == {}
        assert not store.context_tokens_path(credentials.account_id).exists()
        assert not store.processed_messages_path(credentials.account_id).exists()
        assert not store.message_map_path(credentials.account_id).exists()
        assert store.load_applied_inbound_clear_generation() == 3

    assert channel._inbound_clear_active is False
    assert store.load_credentials(account_id=credentials.account_id) == credentials
    assert store.list_account_ids() == [credentials.account_id]
    assert store.load_sync_buf(credentials.account_id) == "provider-cursor"
    assert preserved_settings.read_text(encoding="utf-8") == '{"enabled":true}'
    assert store.load_channel_status() == {
        "state": "running",
        "running": True,
        "configured": True,
        "account_id": credentials.account_id,
    }
    api.get_updates.assert_not_awaited()
    api.get_config.assert_not_awaited()
    api.send_typing.assert_not_awaited()

    async with channel.inbound_clear_boundary(
        ChannelInboundClearRequest(
            channel_type="weixin",
            clear_generation=2,
        )
    ):
        pass
    assert store.load_applied_inbound_clear_generation() == 3


@pytest.mark.parametrize("entry_kind", ["symlink", "hardlink", "fifo"])
def test_clear_removes_indirect_account_content_without_touching_external_data(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    store = WeixinStateStore(str(tmp_path / "state"), credentials=credentials_for_path(str(tmp_path / "state")))
    credentials = WeixinCredentials(account_id="bot@im.bot", token="secret-token")
    store.save_credentials(credentials)
    store.save_sync_buf(credentials.account_id, "provider-cursor")
    external = tmp_path / f"external-{entry_kind}.json"
    external.write_text('{"private":"external"}', encoding="utf-8")
    original = external.read_bytes()
    derived_paths = (
        store.context_tokens_path(credentials.account_id),
        store.processed_messages_path(credentials.account_id),
        store.message_map_path(credentials.account_id),
    )
    for path in derived_paths:
        _make_indirect_entry(path, external, entry_kind)
    unrelated_backup = store.accounts_dir / "bot_backup.context-tokens.json.bak"
    unrelated_backup.write_text('{"preserve":true}', encoding="utf-8")

    store.clear_inbound_content(clear_generation=4)

    assert all(not os.path.lexists(path) for path in derived_paths)
    assert external.read_bytes() == original
    assert store.load_credentials(account_id=credentials.account_id) == credentials
    assert store.load_sync_buf(credentials.account_id) == "provider-cursor"
    assert unrelated_backup.read_text(encoding="utf-8") == '{"preserve":true}'


@pytest.mark.parametrize("entry_kind", ["symlink", "hardlink", "fifo"])
def test_clear_replaces_indirect_status_without_reading_external_data(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    store = WeixinStateStore(str(tmp_path / "state"), credentials=credentials_for_path(str(tmp_path / "state")))
    store.state_dir.mkdir(parents=True)
    external = tmp_path / f"external-status-{entry_kind}.json"
    external.write_text(
        json.dumps(
            {
                "state": "running",
                "running": True,
                "configured": True,
                "account_id": "external-account",
                "last_error": "external secret",
            }
        ),
        encoding="utf-8",
    )
    original = external.read_bytes()
    _make_indirect_entry(store.channel_status_path, external, entry_kind)

    store.clear_inbound_content(clear_generation=5)

    assert external.read_bytes() == original
    assert json.loads(store.channel_status_path.read_text(encoding="utf-8")) == {}
    assert store.load_applied_inbound_clear_generation() == 5


@pytest.mark.parametrize("entry_kind", ["symlink", "hardlink", "fifo"])
def test_clear_replaces_indirect_generation_without_reading_external_data(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    store = WeixinStateStore(str(tmp_path / "state"), credentials=credentials_for_path(str(tmp_path / "state")))
    store.state_dir.mkdir(parents=True)
    external = tmp_path / f"external-generation-{entry_kind}.json"
    external.write_text('{"clear_generation":999}', encoding="utf-8")
    original = external.read_bytes()
    _make_indirect_entry(store.inbound_clear_state_path, external, entry_kind)

    assert store.load_applied_inbound_clear_generation() == 0
    store.clear_inbound_content(clear_generation=6)

    assert external.read_bytes() == original
    assert json.loads(
        store.inbound_clear_state_path.read_text(encoding="utf-8")
    ) == {"clear_generation": 6}


def test_clear_rejects_linked_accounts_directory_without_touching_external_data(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    external_accounts = tmp_path / "external-accounts"
    external_accounts.mkdir()
    external_content = external_accounts / "bot.context-tokens.json"
    external_content.write_text('{"private":"external"}', encoding="utf-8")
    original = external_content.read_bytes()
    (state_dir / "accounts").symlink_to(
        external_accounts,
        target_is_directory=True,
    )

    with pytest.raises(UnsafeManagedPathError):
        WeixinStateStore(str(state_dir), credentials=credentials_for_path(str(state_dir))).clear_inbound_content(clear_generation=7)

    assert external_content.read_bytes() == original
    assert not (state_dir / "inbound_clear_state.json").exists()


def test_clear_rejects_linked_state_root_without_touching_external_data(
    tmp_path: Path,
) -> None:
    external_state = tmp_path / "external-state"
    external_accounts = external_state / "accounts"
    external_accounts.mkdir(parents=True)
    external_content = external_accounts / "bot.context-tokens.json"
    external_content.write_text('{"private":"external"}', encoding="utf-8")
    external_status = external_state / "channel_status.json"
    external_status.write_text('{"last_error":"external"}', encoding="utf-8")
    content_before = external_content.read_bytes()
    status_before = external_status.read_bytes()
    linked_state = tmp_path / "linked-state"
    linked_state.symlink_to(external_state, target_is_directory=True)

    with pytest.raises(UnsafeManagedPathError):
        WeixinStateStore(str(linked_state), credentials=credentials_for_path(str(linked_state))).clear_inbound_content(clear_generation=8)

    assert external_content.read_bytes() == content_before
    assert external_status.read_bytes() == status_before
    assert not (external_state / "inbound_clear_state.json").exists()


@pytest.mark.parametrize(
    "account_suffix",
    ["context-tokens", "processed-messages", "message-map"],
)
def test_clear_preserves_single_suffix_named_account_credentials(
    tmp_path: Path,
    account_suffix: str,
) -> None:
    store = WeixinStateStore(str(tmp_path / "state"), credentials=credentials_for_path(str(tmp_path / "state")))
    account_id = f"foo.{account_suffix}"
    credentials = WeixinCredentials(account_id=account_id, token="secret-token")
    store.save_credentials(credentials)
    store.save_sync_buf(account_id, "provider-cursor")
    store.save_context_tokens(account_id, {"user": "private-context"})
    store.save_processed_message_ids(account_id, {"private-message"})
    store.save_message_id_mapping(account_id, "external", "internal")
    credential_before = store.credentials.get("account")
    cursor_before = store.sync_path(account_id).read_bytes()

    store.clear_inbound_content(clear_generation=9)

    assert store.credentials.get("account") == credential_before
    assert store.sync_path(account_id).read_bytes() == cursor_before
    assert not store.context_tokens_path(account_id).exists()
    assert not store.processed_messages_path(account_id).exists()
    assert not store.message_map_path(account_id).exists()
    assert store.load_applied_inbound_clear_generation() == 9


@pytest.mark.parametrize(
    ("account_suffix", "state_kind"),
    [
        ("context-tokens", "context tokens"),
        ("processed-messages", "processed messages"),
        ("message-map", "message map"),
    ],
)
def test_credentials_do_not_share_content_file_namespace(
    tmp_path: Path, account_suffix: str, state_kind: str,
) -> None:
    store = WeixinStateStore(tmp_path / "state", credentials=credentials_for_path(tmp_path))
    credentials = WeixinCredentials(account_id=f"foo.{account_suffix}", token="vault-secret")
    store.save_credentials(credentials)
    store.save_context_tokens(credentials.account_id, {"user": "private-context"})
    store.clear_inbound_content(clear_generation=10)
    assert store.load_credentials() == credentials
    assert not list(store.state_dir.rglob(f"{credentials.account_id}.json"))
    assert not store.context_tokens_path(credentials.account_id).exists()


@pytest.mark.asyncio
async def test_clear_does_not_wait_for_blocked_host_call_and_pauses_new_messages(
    tmp_path: Path,
) -> None:
    channel, dispatcher, _, _ = _wired_channel(tmp_path)
    first_host_call_started = asyncio.Event()
    release_first_host_call = asyncio.Event()
    clear_entered = asyncio.Event()
    release_clear = asyncio.Event()
    capture_count = 0
    dispatched: list[str] = []

    async def capture(**kwargs):
        nonlocal capture_count
        capture_count += 1
        if capture_count == 1:
            first_host_call_started.set()
            await release_first_host_call.wait()
            raise ChannelInboundRejectedError(
                ChannelInboundRejectionReason.CLEARED_MESSAGE,
                "host clear boundary rejected old work",
            )
        return _context(
            kwargs["evidence"],
            generation=channel._applied_inbound_clear_generation,
        )

    async def dispatch(**kwargs):
        dispatched.append(kwargs["message"])
        return ChannelMessageDispatchOutcome(
            success=True,
            user_id="local-user",
            session_id="session-1",
        )

    dispatcher.capture_inbound_context.side_effect = capture
    dispatcher.dispatch_user_message.side_effect = dispatch

    async def run_clear() -> None:
        async with channel.inbound_clear_boundary(
            ChannelInboundClearRequest(
                channel_type="weixin",
                clear_generation=1,
            )
        ):
            clear_entered.set()
            await release_clear.wait()

    first = asyncio.create_task(
        channel._process_inbound_message(_message("first", message_id="first"))
    )
    await asyncio.wait_for(first_host_call_started.wait(), timeout=1)
    clear_task = asyncio.create_task(run_clear())
    await asyncio.wait_for(clear_entered.wait(), timeout=1)

    second = asyncio.create_task(
        channel._process_inbound_message(
            _message(
                "second",
                message_id="second",
                create_time_ms=PROVIDER_TIME_MS + 1,
            )
        )
    )
    await asyncio.sleep(0)
    assert capture_count == 1

    release_first_host_call.set()
    await asyncio.wait_for(first, timeout=1)
    assert capture_count == 1
    assert dispatched == []

    release_clear.set()
    await asyncio.wait_for(clear_task, timeout=1)
    await asyncio.wait_for(second, timeout=1)
    assert capture_count == 2
    assert dispatched == ["second"]


@pytest.mark.asyncio
async def test_clear_during_typing_ticket_fetch_cannot_restore_cache(
    tmp_path: Path,
) -> None:
    channel, dispatcher, _, _ = _wired_channel(tmp_path)
    channel._config.enable_typing_indicator = True
    request_started = asyncio.Event()
    release_request = asyncio.Event()

    async def get_config(**_kwargs):
        request_started.set()
        await release_request.wait()
        return {"ret": 0, "typing_ticket": "stale-private-ticket"}

    api = MagicMock()
    api.get_config = AsyncMock(side_effect=get_config)
    api.send_typing = AsyncMock()
    channel._api = api
    dispatcher.dispatch_user_message.side_effect = ChannelInboundRejectedError(
        ChannelInboundRejectionReason.CLEARED_MESSAGE,
        "host rejected work that crossed clear",
    )

    message_task = asyncio.create_task(channel._process_inbound_message(_message()))
    await asyncio.wait_for(request_started.wait(), timeout=1)
    async with channel.inbound_clear_boundary(
        ChannelInboundClearRequest(
            channel_type="weixin",
            clear_generation=1,
        )
    ):
        pass

    release_request.set()
    assert await asyncio.wait_for(message_task, timeout=1) is True
    assert channel._typing_cache == {}
    api.send_typing.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_during_poll_processing_cannot_restore_processed_state(
    tmp_path: Path,
) -> None:
    channel, dispatcher, _, _ = _wired_channel(tmp_path)
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()
    clear_entered = asyncio.Event()

    async def dispatch(**_kwargs):
        dispatch_started.set()
        await release_dispatch.wait()
        raise ChannelInboundRejectedError(
            ChannelInboundRejectionReason.CLEARED_MESSAGE,
            "event crossed clear during dispatch",
        )

    dispatcher.dispatch_user_message.side_effect = dispatch
    channel._api = _OneShotUpdatesApi(
        channel,
        {
            "ret": 0,
            "get_updates_buf": "cursor-after-processing",
            "msgs": [_message()],
        },
    )

    async def run_clear() -> None:
        async with channel.inbound_clear_boundary(
            ChannelInboundClearRequest(
                channel_type="weixin",
                clear_generation=1,
            )
        ):
            clear_entered.set()

    poll_task = asyncio.create_task(channel._poll_loop())
    await asyncio.wait_for(dispatch_started.wait(), timeout=1)
    clear_task = asyncio.create_task(run_clear())
    await asyncio.wait_for(clear_entered.wait(), timeout=1)

    release_dispatch.set()
    await asyncio.wait_for(clear_task, timeout=1)
    await asyncio.wait_for(poll_task, timeout=1)

    store = WeixinStateStore(str(tmp_path), credentials=credentials_for_path(str(tmp_path)))
    assert store.load_sync_buf("bot@im.bot") == ""
    assert store.load_processed_message_ids("bot@im.bot") == set()


@pytest.mark.asyncio
async def test_clear_does_not_wait_for_network_poll_or_persist_stale_response(
    tmp_path: Path,
) -> None:
    channel, dispatcher, _, _ = _wired_channel(tmp_path)
    store = WeixinStateStore(str(tmp_path), credentials=credentials_for_path(str(tmp_path)))
    store.save_processed_message_ids("bot@im.bot", {"pre-clear-message"})
    dispatcher.capture_inbound_context.side_effect = ChannelInboundRejectedError(
        ChannelInboundRejectionReason.CLEARED_MESSAGE,
        "old batch returned after clear",
    )
    poll_started = asyncio.Event()
    release_poll = asyncio.Event()
    clear_entered = asyncio.Event()
    release_clear = asyncio.Event()

    class _BlockingApi:
        async def get_updates(self, *, get_updates_buf: str, timeout_ms: int):
            _ = get_updates_buf, timeout_ms
            poll_started.set()
            await release_poll.wait()
            if channel._stop_event is not None:
                channel._stop_event.set()
            return {
                "ret": 0,
                "get_updates_buf": "cursor-after-clear",
                "msgs": [_message("after clear")],
            }

    channel._api = _BlockingApi()

    async def run_clear() -> None:
        async with channel.inbound_clear_boundary(
            ChannelInboundClearRequest(
                channel_type="weixin",
                clear_generation=1,
            )
        ):
            clear_entered.set()
            await release_clear.wait()

    poll_task = asyncio.create_task(channel._poll_loop())
    await asyncio.wait_for(poll_started.wait(), timeout=1)
    clear_task = asyncio.create_task(run_clear())
    await asyncio.wait_for(clear_entered.wait(), timeout=1)

    release_poll.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    dispatcher.capture_inbound_context.assert_not_awaited()
    assert WeixinStateStore(str(tmp_path), credentials=credentials_for_path(str(tmp_path))).load_sync_buf("bot@im.bot") == ""

    release_clear.set()
    await asyncio.wait_for(clear_task, timeout=1)
    await asyncio.wait_for(poll_task, timeout=1)
    dispatcher.capture_inbound_context.assert_not_awaited()
    assert store.load_sync_buf("bot@im.bot") == ""
    assert store.load_processed_message_ids("bot@im.bot") == set()


@pytest.mark.asyncio
async def test_api_error_from_pre_clear_poll_cannot_restore_last_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel, _, _, _ = _wired_channel(tmp_path)
    poll_started = asyncio.Event()
    release_poll = asyncio.Event()

    class _BlockingErrorApi:
        async def get_updates(self, *, get_updates_buf: str, timeout_ms: int):
            _ = get_updates_buf, timeout_ms
            poll_started.set()
            await release_poll.wait()
            assert channel._stop_event is not None
            channel._stop_event.set()
            return {
                "ret": -1,
                "errmsg": "private provider error from stale poll",
            }

    channel._api = _BlockingErrorApi()
    handle_api_error = MagicMock(return_value=2_000)
    monkeypatch.setattr(channel, "_handle_api_error", handle_api_error)

    poll_task = asyncio.create_task(channel._poll_loop())
    await asyncio.wait_for(poll_started.wait(), timeout=1)
    async with channel.inbound_clear_boundary(
        ChannelInboundClearRequest(
            channel_type="weixin",
            clear_generation=1,
        )
    ):
        pass
    release_poll.set()
    await asyncio.wait_for(poll_task, timeout=1)

    handle_api_error.assert_not_called()
    status = WeixinStateStore(str(tmp_path), credentials=credentials_for_path(str(tmp_path))).load_channel_status()
    assert "last_error" not in status
    assert "last_error_at_ms" not in status
