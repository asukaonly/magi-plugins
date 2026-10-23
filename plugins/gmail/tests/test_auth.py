"""PKCE, callback binding, refresh and sanitized error boundaries."""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from gmail.auth import OAuthSession
from gmail.client import SCOPE, TOKEN_KEY, GmailClient


class Vault:
    def __init__(self):
        self.values = {}
    def get(self, key):
        return self.values.get(key)
    def set(self, key, value):
        self.values[key] = value
    def delete(self, key):
        self.values.pop(key, None)


@pytest.mark.asyncio
async def test_loopback_callback_rejects_wrong_state_and_uses_pkce():
    session = OAuthSession("client.apps.googleusercontent.com")
    url = await session.start()
    try:
        params = parse_qs(urlsplit(url).query)
        assert params["scope"] == [SCOPE] and params["code_challenge_method"] == ["S256"]
        assert session.verifier not in url
        async with httpx.AsyncClient(trust_env=False) as http:
            bad = await http.get(session.redirect_uri, params={"code": "stolen", "state": "wrong"})
            assert bad.status_code == 400 and session.code is None
            good = await http.get(session.redirect_uri, params={"code": "valid", "state": session.state})
            assert good.status_code == 200 and session.code == "valid"
        client = AsyncMock()
        client.request_json.return_value = {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600, "scope": SCOPE}
        result = await session.exchange(client)
        assert result["refresh_token"] == "refresh"
        assert client.request_json.call_args.kwargs["data"]["code_verifier"] == session.verifier
        assert client.request_json.call_args.kwargs["data"]["redirect_uri"] == session.redirect_uri
    finally:
        await session.close()
    assert not session.verifier and not session.code


@pytest.mark.asyncio
async def test_missing_scope_or_refresh_token_cannot_complete_setup():
    session = OAuthSession("client")
    session.code = "valid"
    client = AsyncMock()
    client.request_json.return_value = {"access_token": "access", "expires_in": 3600, "scope": "other"}
    with pytest.raises(PermissionError):
        await session.exchange(client)


@pytest.mark.asyncio
async def test_refresh_is_serialized_and_never_exposes_tokens_in_results():
    vault = Vault()
    vault.set(TOKEN_KEY, json.dumps({"client_id": "client", "refresh_token": "secret-refresh", "expires_at": 0}))
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "secret-access", "expires_in": 3600, "scope": SCOPE})
        assert request.headers["authorization"] == "Bearer secret-access"
        return httpx.Response(200, json={"historyId": "12"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GmailClient(vault, http=http)
        results = await asyncio.gather(client.get("/profile"), client.get("/profile"))
    assert len([c for c in calls if c.url.path == "/token"]) == 1
    assert results == [{"historyId": "12"}, {"historyId": "12"}]
    assert "secret" not in str(results)


@pytest.mark.asyncio
async def test_revoked_refresh_returns_reconnect_error_without_provider_body():
    vault = Vault()
    vault.set(TOKEN_KEY, json.dumps({"client_id": "client", "refresh_token": "secret-refresh"}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(400, json={"error": "secret-refresh"}))) as http:
        client = GmailClient(vault, http=http)
        with pytest.raises(PermissionError, match="reconnect") as error:
            await client.get("/profile")
        assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_rejected_access_token_refreshes_once():
    vault = Vault()
    vault.set(TOKEN_KEY, json.dumps({"client_id": "client", "refresh_token": "r", "access_token": "old", "expires_at": time.time() + 3600}))
    def handler(request):
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
        return httpx.Response(401) if request.headers["authorization"] == "Bearer old" else httpx.Response(200, json={"historyId": "12"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await GmailClient(vault, http=http).get("/profile") == {"historyId": "12"}


@pytest.mark.asyncio
async def test_disconnect_during_refresh_cannot_restore_the_deleted_token():
    vault = Vault()
    vault.set(TOKEN_KEY, json.dumps({"client_id": "client", "refresh_token": "r"}))
    async def handler(request):
        vault.delete(TOKEN_KEY)
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PermissionError):
            await GmailClient(vault, http=http).access_token()
    assert vault.get(TOKEN_KEY) is None
