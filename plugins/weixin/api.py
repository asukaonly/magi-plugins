"""HTTP client for the Weixin iLink bot gateway."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
DEFAULT_BOT_TYPE = "3"
DEFAULT_LONG_POLL_TIMEOUT_MS = 35_000
DEFAULT_API_TIMEOUT_MS = 15_000
DEFAULT_CONFIG_TIMEOUT_MS = 10_000
SESSION_EXPIRED_ERRCODE = -14

MESSAGE_TYPE_USER = 1
MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2
MESSAGE_ITEM_TEXT = 1
MESSAGE_ITEM_IMAGE = 2
MESSAGE_ITEM_VOICE = 3
MESSAGE_ITEM_FILE = 4
MESSAGE_ITEM_VIDEO = 5
TYPING_STATUS_TYPING = 1


class WeixinApiError(RuntimeError):
    """Raised when the Weixin gateway returns an HTTP or protocol error."""


class WeixinApiTimeout(TimeoutError):
    """Raised when an HTTP request times out."""


def _build_client_version(version: str) -> int:
    parts = []
    for raw in version.split(".")[:3]:
        try:
            parts.append(int(raw))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[:3]
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def _random_wechat_uin() -> str:
    value = str(secrets.randbits(32)).encode("utf-8")
    return base64.b64encode(value).decode("ascii")


def _is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, socket.timeout):
        return True
    return False


def _ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else f"{url}/"


@dataclass(slots=True)
class WeixinApiClient:
    """Minimal async wrapper around the iLink bot HTTP JSON API."""

    base_url: str = DEFAULT_BASE_URL
    token: str = ""
    channel_version: str = "0.1.0"
    ilink_app_id: str = "bot"
    route_tag: str = ""

    async def get_qr_code(self, *, bot_type: str = DEFAULT_BOT_TYPE) -> dict[str, Any]:
        endpoint = f"ilink/bot/get_bot_qrcode?bot_type={urllib.parse.quote(bot_type)}"
        return await self._request_json("GET", endpoint, timeout_ms=DEFAULT_API_TIMEOUT_MS)

    async def get_qr_status(self, *, qrcode: str, timeout_ms: int) -> dict[str, Any]:
        endpoint = f"ilink/bot/get_qrcode_status?qrcode={urllib.parse.quote(qrcode)}"
        return await self._request_json("GET", endpoint, timeout_ms=timeout_ms)

    async def get_updates(
        self,
        *,
        get_updates_buf: str,
        timeout_ms: int = DEFAULT_LONG_POLL_TIMEOUT_MS,
    ) -> dict[str, Any]:
        try:
            return await self._request_json(
                "POST",
                "ilink/bot/getupdates",
                body={
                    "get_updates_buf": get_updates_buf or "",
                    "base_info": self._base_info(),
                },
                timeout_ms=timeout_ms,
            )
        except WeixinApiTimeout:
            return {"ret": 0, "msgs": [], "get_updates_buf": get_updates_buf or ""}

    async def send_text_message(
        self,
        *,
        to_user_id: str,
        text: str,
        context_token: str | None,
        timeout_ms: int = DEFAULT_API_TIMEOUT_MS,
    ) -> str:
        client_id = f"magi-weixin-{secrets.token_hex(12)}"
        response = await self._request_json(
            "POST",
            "ilink/bot/sendmessage",
            body={
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": client_id,
                    "message_type": MESSAGE_TYPE_BOT,
                    "message_state": MESSAGE_STATE_FINISH,
                    "item_list": [
                        {
                            "type": MESSAGE_ITEM_TEXT,
                            "text_item": {"text": text},
                        }
                    ],
                    "context_token": context_token or None,
                },
                "base_info": self._base_info(),
            },
            timeout_ms=timeout_ms,
        )
        ret = response.get("ret")
        errcode = response.get("errcode")
        if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
            message = response.get("errmsg") or response.get("message") or response
            raise WeixinApiError(f"Weixin sendmessage returned an error: {message}")
        return client_id

    async def get_config(
        self,
        *,
        ilink_user_id: str,
        context_token: str | None,
    ) -> dict[str, Any]:
        return await self._request_json(
            "POST",
            "ilink/bot/getconfig",
            body={
                "ilink_user_id": ilink_user_id,
                "context_token": context_token,
                "base_info": self._base_info(),
            },
            timeout_ms=DEFAULT_CONFIG_TIMEOUT_MS,
        )

    async def send_typing(
        self,
        *,
        ilink_user_id: str,
        typing_ticket: str,
        status: int = TYPING_STATUS_TYPING,
    ) -> None:
        await self._request_json(
            "POST",
            "ilink/bot/sendtyping",
            body={
                "ilink_user_id": ilink_user_id,
                "typing_ticket": typing_ticket,
                "status": status,
                "base_info": self._base_info(),
            },
            timeout_ms=DEFAULT_CONFIG_TIMEOUT_MS,
        )

    def with_token(self, token: str, base_url: str | None = None) -> "WeixinApiClient":
        return WeixinApiClient(
            base_url=base_url or self.base_url,
            token=token,
            channel_version=self.channel_version,
            ilink_app_id=self.ilink_app_id,
            route_tag=self.route_tag,
        )

    def _base_info(self) -> dict[str, str]:
        return {"channel_version": self.channel_version}

    def _common_headers(self) -> dict[str, str]:
        headers = {
            "iLink-App-Id": self.ilink_app_id,
            "iLink-App-ClientVersion": str(_build_client_version(self.channel_version)),
        }
        if self.route_tag:
            headers["SKRouteTag"] = self.route_tag
        return headers

    async def _request_json(
        self,
        method: str,
        endpoint: str,
        *,
        body: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._request_json_sync,
            method,
            endpoint,
            body,
            timeout_ms,
        )

    def _request_json_sync(
        self,
        method: str,
        endpoint: str,
        body: dict[str, Any] | None,
        timeout_ms: int | None,
    ) -> dict[str, Any]:
        base = _ensure_trailing_slash(self.base_url or DEFAULT_BASE_URL)
        url = urllib.parse.urljoin(base, endpoint)
        headers = self._common_headers()
        data: bytes | None = None

        if method.upper() == "POST":
            data = json.dumps(body or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers.update(
                {
                    "Content-Type": "application/json",
                    "AuthorizationType": "ilink_bot_token",
                    "Content-Length": str(len(data)),
                    "X-WECHAT-UIN": _random_wechat_uin(),
                }
            )
            if self.token.strip():
                headers["Authorization"] = f"Bearer {self.token.strip()}"

        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        timeout_s = (timeout_ms / 1000) if timeout_ms and timeout_ms > 0 else None

        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise WeixinApiError(f"Weixin API HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            if _is_timeout_error(exc):
                raise WeixinApiTimeout(str(exc)) from exc
            raise

        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WeixinApiError(f"Weixin API returned invalid JSON: {raw[:200]}") from exc
        if not isinstance(parsed, dict):
            raise WeixinApiError("Weixin API returned a non-object JSON response")
        return parsed
