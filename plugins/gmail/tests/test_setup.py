"""Connection isolation and cancellation at the real settings-action boundary."""
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from gmail.client import TOKEN_KEY
from sdk_test_support import bind_test_plugin, load_plugin


def plugin(connection="work", email=""):
    return bind_test_plugin(load_plugin(Path(__file__).parents[1] / "plugin.toml"),
        connection_id=connection, settings={"sources": {"mail": {"account_email": email}}})


def session():
    result = AsyncMock()
    result.code = "code"
    result.error = None
    result.client_id = "client"
    result.client_secret = "secret"
    result.exchange.return_value = {"access_token": "a", "refresh_token": "r", "client_id": "client"}
    return result


@pytest.mark.asyncio
async def test_reauthorization_cannot_change_mailbox_identity():
    instance = plugin(email="work@example.com")
    auth = session()
    instance._sessions["s"] = auth
    instance._client = AsyncMock()
    instance._client.request_json.return_value = {"emailAddress": "personal@example.com"}
    result = await instance.poll_settings_action("connect", session_id="s")
    assert result.status == "failed" and instance.context.credentials.get(TOKEN_KEY) is None
    auth.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_authorization_cannot_restore_credentials():
    instance = plugin()
    auth = session()
    instance._sessions["s"] = auth
    instance._client = AsyncMock()
    async def profile(*args, **kwargs):
        await instance.cancel_settings_action("connect", session_id="s")
        return {"emailAddress": "work@example.com"}
    instance._client.request_json.side_effect = profile
    result = await instance.poll_settings_action("connect", session_id="s")
    assert result.status == "cancelled"
    assert instance.context.credentials.get(TOKEN_KEY) is None


@pytest.mark.asyncio
async def test_disconnect_is_local_and_does_not_touch_another_account():
    first, second = plugin("work"), plugin("personal")
    first.context.credentials.set(TOKEN_KEY, "work-token")
    second.context.credentials.set(TOKEN_KEY, "personal-token")
    first._client = AsyncMock()
    client = first._client
    result = await first.start_settings_action("disconnect", session_id="s")
    assert result.status == "succeeded" and result.settings_updates["sources.mail.enabled"] is False
    assert first.context.credentials.get(TOKEN_KEY) is None
    assert second.context.credentials.get(TOKEN_KEY) == "personal-token"
    client.close.assert_awaited_once()
    client.request_json.assert_not_called()
