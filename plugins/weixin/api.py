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

# === Upload media-type enum — DIFFERENT enum from MESSAGE_ITEM_* ===
# These are for the ``media_type`` field in ``getuploadurl`` request
# bodies. openclaw-weixin (``src/api/types.ts:25-30``) defines:
#   UploadMediaType = { IMAGE: 1, VIDEO: 2, FILE: 3, VOICE: 4 }
# Note IMAGE=1 here, NOT 2 — collision with MessageItemType.IMAGE=2
# (which is for ``item_list[].type`` in sendmessage). Previously this
# plugin reused MESSAGE_ITEM_IMAGE=2 for the upload media_type call,
# which made iLink interpret image uploads as VIDEO uploads — the
# server then 500'd with cryptic ``x-error-code: -5102031`` because
# the cipher payload didn't match the VIDEO-pipeline validations.
UPLOAD_MEDIA_TYPE_IMAGE = 1
UPLOAD_MEDIA_TYPE_VIDEO = 2
UPLOAD_MEDIA_TYPE_FILE = 3
UPLOAD_MEDIA_TYPE_VOICE = 4
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

    # === Media upload (Phase A media-outbound) =============================
    #
    # The iLink image-send protocol is a 3-step dance:
    #
    #     1. POST ilink/bot/getuploadurl with plaintext + ciphertext sizes,
    #        plaintext MD5, and the (base64) AES-128 key. Server replies
    #        with ``upload_param`` (an opaque CDN credentials string),
    #        ``upload_full_url`` (the CDN PUT endpoint), and the same for
    #        the thumbnail.
    #     2. PUT the AES-128-ECB-encrypted bytes to ``upload_full_url``
    #        (and the encrypted thumbnail to its own URL).
    #     3. POST ilink/bot/sendmessage with an item_list entry of
    #        ``type=2 (IMAGE)`` whose ``image_item.media`` carries the
    #        ``encrypt_query_param`` (= upload_param from step 1) and
    #        ``aes_key`` (= base64 of the AES key) so the recipient can
    #        decrypt.
    #
    # See ``media_upload.py`` for the crypto/thumbnail helpers that feed
    # this flow, and ``adapter.py::_send_image_attachment`` for the
    # orchestrator that ties getuploadurl → CDN PUT → send_image_message
    # together for a single attachment dict.

    async def get_upload_url(
        self,
        *,
        filekey: str,
        media_type: int,
        to_user_id: str,
        raw_size: int,
        raw_md5: str,
        cipher_size: int,
        thumb_raw_size: int,
        thumb_raw_md5: str,
        thumb_cipher_size: int,
        aes_key_hex: str,
        no_need_thumb: bool = False,
        timeout_ms: int = DEFAULT_API_TIMEOUT_MS,
    ) -> dict[str, Any]:
        """Step 1 of the image-send flow — get pre-signed CDN params.

        ``raw_*`` describe the plaintext file; ``cipher_size`` is the
        AES-PKCS7-padded ciphertext length the client will POST to
        the CDN.

        ``aes_key_hex`` is the AES-128 key as a 32-char lowercase
        hex string (NOT base64). Verified against openclaw-weixin
        reference (src/cdn/upload.ts:81): ``aeskey:
        aeskey.toString("hex")``. The previous ``aes_key_b64``
        parameter name was misleading and caused the iLink server
        to record an unusable key, so the message-side recipient
        couldn't decrypt the image.
        """
        body: dict[str, Any] = {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": raw_size,
            "rawfilemd5": raw_md5,
            "filesize": cipher_size,
            "aeskey": aes_key_hex,
            "base_info": self._base_info(),
        }
        if no_need_thumb:
            body["no_need_thumb"] = True
        else:
            body["thumb_rawsize"] = thumb_raw_size
            body["thumb_rawfilemd5"] = thumb_raw_md5
            body["thumb_filesize"] = thumb_cipher_size
        response = await self._request_json(
            "POST", "ilink/bot/getuploadurl", body=body, timeout_ms=timeout_ms,
        )
        ret = response.get("ret")
        errcode = response.get("errcode")
        if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
            message = response.get("errmsg") or response.get("message") or response
            raise WeixinApiError(f"Weixin getuploadurl returned an error: {message}")
        return response

    async def upload_to_cdn(
        self,
        *,
        upload_full_url: str | None,
        upload_param: str | None,
        filekey: str,
        cdn_base_url: str,
        encrypted_bytes: bytes,
        timeout_ms: int = DEFAULT_API_TIMEOUT_MS,
    ) -> str:
        """Step 2 — POST AES-encrypted bytes to the CDN, return the
        download token (``x-encrypted-param`` response header).

        Protocol verified against Tencent/openclaw-weixin reference
        (src/cdn/cdn-upload.ts + src/cdn/cdn-url.ts):

        * **Method is POST** (not PUT — earlier comment was wrong).
        * **URL**: prefer server-returned ``upload_full_url``; when
          absent (current iLink shape), build
          ``{cdn_base}/upload?encrypted_query_param={upload_param}
          &filekey={filekey}``.
        * **Body**: opaque AES-128-ECB ciphertext.
        * **Response**: ``x-encrypted-param`` header → the
          ``encrypt_query_param`` value to embed in the subsequent
          sendmessage. This is a DIFFERENT token from the
          ``upload_param`` we just used to upload.
        * **Error handling**: 4xx aborts immediately; 5xx may be
          retried but credentials are single-use, so caller must
          re-call ``get_upload_url`` first. We do not retry here.

        Returns the download_encrypted_query_param (header value)
        — caller passes this into ``send_image_message``'s
        ``image_param`` / ``download_query_param``.
        """
        if upload_full_url and upload_full_url.strip():
            url = upload_full_url.strip()
        elif upload_param:
            url = (
                f"{cdn_base_url.rstrip('/')}/upload"
                f"?encrypted_query_param="
                f"{urllib.parse.quote(upload_param, safe='')}"
                f"&filekey={urllib.parse.quote(filekey, safe='')}"
            )
        else:
            raise WeixinApiError(
                "Weixin CDN upload: neither upload_full_url nor "
                "upload_param available in getuploadurl response"
            )
        return await asyncio.to_thread(
            self._upload_to_cdn_sync, url, encrypted_bytes, timeout_ms,
        )

    def _upload_to_cdn_sync(
        self, url: str, encrypted_bytes: bytes, timeout_ms: int | None,
    ) -> str:
        # Header set chosen to look like a modern browser fetch:
        # the CDN edge may be rejecting Python-urllib/3.x as a bot,
        # and the previous bare {Content-Type, Content-Length}
        # request returned HTTP 500 with x-error-code=-5102031 and
        # empty body. Adding User-Agent / Accept / Accept-Encoding /
        # Connection mirrors what Node's fetch() (used by openclaw)
        # sends by default — same shape the CDN is known to accept.
        # Match openclaw-weixin EXACTLY (src/cdn/cdn-upload.ts:42-46):
        # only Content-Type, nothing else. The previous Chrome-style
        # header set was a guess that didn't help; the actual cause
        # of -5102031 was the wrong upload media_type sent in the
        # PRIOR getuploadurl call (see UPLOAD_MEDIA_TYPE_IMAGE
        # constant in this file). Keeping headers minimal matches
        # the proven-working reference.
        headers = {
            "Content-Type": "application/octet-stream",
        }
        # CDN diagnostic logging: dump the full URL (truncated) so
        # we can verify the host + query params are right. Plugin
        # uses the SDK-provided logger to integrate with backend
        # logging config.
        from magi_plugin_sdk import get_logger
        _log = get_logger("magi_plugin_weixin.api")
        url_for_log = url if len(url) <= 400 else url[:400] + "...(truncated)"
        _log.info(
            "Weixin CDN POST → %s ciphertext_bytes=%d",
            url_for_log, len(encrypted_bytes),
        )

        request = urllib.request.Request(
            url, data=encrypted_bytes, headers=headers, method="POST",
        )
        timeout_s = (timeout_ms / 1000) if timeout_ms and timeout_ms > 0 else None
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                # Read the download token BEFORE draining body — urllib
                # exposes headers via response.headers regardless of
                # body read order, but be explicit.
                download_param = (
                    response.headers.get("x-encrypted-param") or ""
                ).strip()
                resp_body_drained = response.read()
                if not download_param:
                    _log.warning(
                        "Weixin CDN POST 2xx but no x-encrypted-param "
                        "header. resp_headers=%s body_preview=%s",
                        dict(response.headers),
                        resp_body_drained[:200],
                    )
        except urllib.error.HTTPError as exc:
            err_header = ""
            err_body = ""
            try:
                if exc.headers is not None:
                    err_header = (exc.headers.get("x-error-message") or "").strip()
            except Exception:
                pass
            try:
                err_body = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            # Surface BOTH the header and body in the error AND in the
            # log — the 500 we hit had both empty, so we need to see
            # the response headers to understand what the CDN is
            # rejecting (auth? URL shape? POST not supported?).
            try:
                exc_headers_dump = dict(exc.headers) if exc.headers else {}
            except Exception:
                exc_headers_dump = {}
            _log.error(
                "Weixin CDN POST HTTP %s url=%s err_header=%r err_body=%r "
                "resp_headers=%s",
                exc.code, url_for_log, err_header, err_body, exc_headers_dump,
            )
            detail = err_header or err_body or "(no error detail in response)"
            raise WeixinApiError(
                f"Weixin CDN upload POST HTTP {exc.code}: {detail}"
            ) from exc
        except Exception as exc:
            if _is_timeout_error(exc):
                raise WeixinApiTimeout(str(exc)) from exc
            raise

        if not download_param:
            raise WeixinApiError(
                "Weixin CDN upload response missing x-encrypted-param "
                "header (the download token for sendmessage)"
            )
        return download_param

    async def send_image_message(
        self,
        *,
        to_user_id: str,
        image_param: str,
        image_aes_key_b64: str,
        image_size: int,
        thumb_param: str | None,
        thumb_aes_key_b64: str | None,
        thumb_width: int | None,
        thumb_height: int | None,
        thumb_size: int | None,
        context_token: str | None,
        timeout_ms: int = DEFAULT_API_TIMEOUT_MS,
    ) -> str:
        """Step 3 — post the message itself with ``item_list[type=2]``.

        The CDNMedia fields (``encrypt_query_param`` + ``aes_key``) wire
        the recipient back to the ciphertext on the CDN. The thumb
        is optional but Weixin clients render a much better preview
        when it's present, so we provide it whenever possible.
        Returns the client-generated ``client_id`` so callers can build
        a DeliveryReceipt that retract/revise can correlate later.
        """
        client_id = f"magi-weixin-{secrets.token_hex(12)}"
        image_item: dict[str, Any] = {
            "media": {
                "encrypt_query_param": image_param,
                "aes_key": image_aes_key_b64,
            },
            "mid_size": image_size,
            "hd_size": image_size,
        }
        if (
            thumb_param is not None
            and thumb_aes_key_b64 is not None
            and thumb_width is not None
            and thumb_height is not None
            and thumb_size is not None
        ):
            image_item["thumb_media"] = {
                "encrypt_query_param": thumb_param,
                "aes_key": thumb_aes_key_b64,
            }
            image_item["thumb_width"] = thumb_width
            image_item["thumb_height"] = thumb_height
            image_item["thumb_size"] = thumb_size
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
                            "type": MESSAGE_ITEM_IMAGE,
                            "image_item": image_item,
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
            raise WeixinApiError(
                f"Weixin sendmessage(image) returned an error: {message}"
            )
        return client_id

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
