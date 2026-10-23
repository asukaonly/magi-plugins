"""Bounded Gmail HTTP transport and connection-scoped token refresh."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import quote

import httpx

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
TOKEN_KEY = "sources.mail.oauth_token"
SECRET_KEY = "sources.mail.client_secret"
API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class GmailError(RuntimeError):
    """A sanitized provider error; response bodies never enter logs or tool context."""
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Gmail request failed (HTTP {status})")


class GmailClient:
    def __init__(self, credentials: Any, *, http: httpx.AsyncClient | None = None) -> None:
        self.credentials = credentials
        self._http = http
        self._owns_http = http is None
        self._refresh_lock = asyncio.Lock()
        self._closed = False

    async def close(self) -> None:
        self._closed = True
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    async def request_json(self, method: str, url: str, **kwargs: Any) -> dict:
        if self._closed:
            raise PermissionError("Gmail connection was closed")
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10, follow_redirects=False)
        for attempt in range(2):
            async with self._http.stream(method, url, **kwargs) as response:
                if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                if response.status_code >= 300:
                    raise GmailError(response.status_code)
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 4 * 1024 * 1024:
                        raise ValueError("Gmail response exceeds the read budget")
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise TypeError("Gmail response must be an object")
                return value
        raise GmailError(503)

    def token(self) -> dict:
        if self._closed:
            raise PermissionError("Gmail connection was closed")
        raw = self.credentials.get(TOKEN_KEY)
        if not raw:
            raise PermissionError("Connect Gmail before reading mail")
        token = json.loads(raw)
        if not isinstance(token, dict) or not token.get("refresh_token") or not token.get("client_id"):
            raise PermissionError("Reconnect Gmail to restore authorization")
        return token

    async def access_token(self, *, rejected_token: str | None = None) -> str:
        async with self._refresh_lock:
            token = self.token()
            access = str(token.get("access_token") or "")
            if access and float(token.get("expires_at") or 0) > time.time() + 60 and access != rejected_token:
                return access
            form = {"client_id": token["client_id"], "refresh_token": token["refresh_token"], "grant_type": "refresh_token"}
            secret = token.get("client_secret")
            if secret:
                form["client_secret"] = secret
            try:
                fresh = await self.request_json("POST", TOKEN_URL, data=form)
            except GmailError as exc:
                if exc.status in {400, 401, 403}:
                    raise PermissionError("Gmail authorization expired; reconnect the mailbox") from None
                raise
            if not fresh.get("access_token") or ("scope" in fresh and SCOPE not in fresh["scope"].split()):
                raise PermissionError("Gmail read permission is missing")
            current = self.token()
            if any(current.get(key) != token.get(key) for key in ("client_id", "refresh_token", "email")):
                raise PermissionError("Gmail authorization changed during refresh")
            token.update(access_token=fresh["access_token"], expires_at=time.time() + float(fresh["expires_in"]))
            if fresh.get("refresh_token"):
                token["refresh_token"] = fresh["refresh_token"]
            self.credentials.set(TOKEN_KEY, json.dumps(token))
            return str(token["access_token"])

    async def get(self, path: str, *, params: dict | None = None) -> dict:
        access = await self.access_token()
        for attempt in range(2):
            try:
                return await self.request_json("GET", API_ROOT + path,
                    params=params, headers={"Authorization": f"Bearer {access}"})
            except GmailError as exc:
                if exc.status == 401 and attempt == 0:
                    access = await self.access_token(rejected_token=access)
                    continue
                if exc.status in {401, 403}:
                    raise PermissionError("Gmail read access is unavailable; check the connection") from None
                raise
        raise PermissionError("Gmail read access is unavailable")

    async def message(self, object_id: str) -> dict:
        return await self.get("/messages/" + quote(object_id, safe=""), params={"format": "full"})
