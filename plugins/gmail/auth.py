"""Desktop OAuth PKCE with a bounded, state-checked loopback callback."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from .client import SCOPE, TOKEN_URL, GmailClient


class OAuthSession:
    def __init__(self, client_id: str, client_secret: str = "") -> None:
        self.client_id, self.client_secret = client_id, client_secret
        self.state = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(64)
        self.code: str | None = None
        self.error: str | None = None
        self.deadline = time.monotonic() + 600
        self.server: asyncio.Server | None = None
        self.expiry: asyncio.TimerHandle | None = None
        self.redirect_uri = ""

    async def start(self) -> str:
        self.server = await asyncio.start_server(self._callback, "127.0.0.1", 0, limit=8192)
        port = self.server.sockets[0].getsockname()[1]
        self.redirect_uri = f"http://127.0.0.1:{port}/oauth/callback"
        self.expiry = asyncio.get_running_loop().call_later(600, self._expire)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip("=")
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri,
            "response_type": "code", "scope": SCOPE, "state": self.state,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "access_type": "offline", "prompt": "consent select_account",
        })

    def _expire(self) -> None:
        self.error = "Gmail authorization expired; start again"
        if self.server is not None:
            self.server.close()

    async def close(self) -> None:
        if self.expiry:
            self.expiry.cancel()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        self.verifier = ""
        self.code = None
        self.client_secret = ""

    async def _callback(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status, body = "400 Bad Request", "Invalid authorization callback."
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            method, target, _ = header.split(b"\r\n", 1)[0].decode("ascii").split(" ", 2)
            url = urlsplit(target)
            query = parse_qs(url.query)
            state = query.get("state", [""])
            if (method == "GET" and url.path == "/oauth/callback" and len(state) == 1
                    and secrets.compare_digest(state[0], self.state) and time.monotonic() < self.deadline
                    and self.code is None and self.error is None):
                if query.get("error"):
                    self.error = "Gmail authorization was declined"
                elif len(query.get("code", [])) == 1 and query["code"][0]:
                    self.code = query["code"][0]
                    status, body = "200 OK", "Gmail authorization received. Return to Magi to finish connecting."
                if self.server:
                    self.server.close()
            writer.write((f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body.encode())}\r\nCache-Control: no-store\r\n"
                "Content-Security-Policy: default-src 'none'\r\nConnection: close\r\n\r\n" + body).encode())
            await writer.drain()
        except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def exchange(self, client: GmailClient) -> dict:
        if self.error or time.monotonic() >= self.deadline or not self.code:
            raise PermissionError(self.error or "Gmail authorization is incomplete or expired")
        form = {"client_id": self.client_id, "code": self.code, "code_verifier": self.verifier,
                "redirect_uri": self.redirect_uri, "grant_type": "authorization_code"}
        if self.client_secret:
            form["client_secret"] = self.client_secret
        token = await client.request_json("POST", TOKEN_URL, data=form)
        if (not token.get("access_token") or not token.get("refresh_token")
                or SCOPE not in str(token.get("scope") or "").split()):
            raise PermissionError("Gmail read-only permission and offline access are required")
        return {"client_id": self.client_id, "client_secret": self.client_secret, "access_token": token["access_token"],
            "refresh_token": token["refresh_token"], "expires_at": time.time() + float(token["expires_in"])}
