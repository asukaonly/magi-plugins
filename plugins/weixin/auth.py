"""QR login helper for the Weixin iLink gateway."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass

from .api import DEFAULT_BASE_URL, DEFAULT_BOT_TYPE, WeixinApiClient
from .state import WeixinCredentials, WeixinStateStore


QR_LONG_POLL_TIMEOUT_MS = 35_000
DEFAULT_LOGIN_TIMEOUT_MS = 480_000
MAX_QR_REFRESH_COUNT = 3


@dataclass(slots=True)
class WeixinLoginResult:
    """Result returned by a QR login attempt."""

    connected: bool
    message: str
    account_id: str = ""
    token: str = ""
    base_url: str = DEFAULT_BASE_URL
    user_id: str = ""


async def login_with_qr(
    *,
    state_store: WeixinStateStore,
    base_url: str = DEFAULT_BASE_URL,
    bot_type: str = DEFAULT_BOT_TYPE,
    timeout_ms: int = DEFAULT_LOGIN_TIMEOUT_MS,
    print_qr_url: bool = True,
) -> WeixinLoginResult:
    """Run the QR login flow and persist credentials on success."""

    client = WeixinApiClient(base_url=base_url or DEFAULT_BASE_URL)
    qrcode_payload = await client.get_qr_code(bot_type=bot_type)
    qrcode = str(qrcode_payload.get("qrcode") or "")
    qrcode_url = str(qrcode_payload.get("qrcode_img_content") or "")
    if not qrcode or not qrcode_url:
        return WeixinLoginResult(False, "The Weixin gateway did not return a QR code.")

    if print_qr_url:
        print("Scan this QR code link with Weixin:")
        print(qrcode_url)

    deadline = time.monotonic() + max(timeout_ms, 1000) / 1000
    refresh_count = 1
    current_base_url = DEFAULT_BASE_URL
    scanned_printed = False

    while time.monotonic() < deadline:
        status = await client.with_token("", current_base_url).get_qr_status(
            qrcode=qrcode,
            timeout_ms=QR_LONG_POLL_TIMEOUT_MS,
        )
        status_name = str(status.get("status") or "wait")
        if status_name == "wait":
            await asyncio.sleep(1)
            continue
        if status_name == "scaned":
            if print_qr_url and not scanned_printed:
                print("QR code scanned. Confirm the login in Weixin.")
                scanned_printed = True
            await asyncio.sleep(1)
            continue
        if status_name == "scaned_but_redirect":
            redirect_host = str(status.get("redirect_host") or "").strip()
            if redirect_host:
                current_base_url = f"https://{redirect_host}"
            await asyncio.sleep(1)
            continue
        if status_name == "expired":
            refresh_count += 1
            if refresh_count > MAX_QR_REFRESH_COUNT:
                return WeixinLoginResult(False, "The QR code expired too many times.")
            qrcode_payload = await client.get_qr_code(bot_type=bot_type)
            qrcode = str(qrcode_payload.get("qrcode") or "")
            qrcode_url = str(qrcode_payload.get("qrcode_img_content") or "")
            if print_qr_url:
                print("QR code refreshed. Scan this new link:")
                print(qrcode_url)
            scanned_printed = False
            await asyncio.sleep(1)
            continue
        if status_name == "confirmed":
            token = str(status.get("bot_token") or "").strip()
            account_id = str(status.get("ilink_bot_id") or "").strip()
            if not token or not account_id:
                return WeixinLoginResult(False, "Login confirmed, but account credentials were missing.")
            credentials = WeixinCredentials(
                account_id=account_id,
                token=token,
                base_url=str(status.get("baseurl") or current_base_url or DEFAULT_BASE_URL),
                user_id=str(status.get("ilink_user_id") or ""),
            )
            saved_path = state_store.save_credentials(credentials)
            return WeixinLoginResult(
                True,
                f"Weixin login succeeded. Credentials saved to {saved_path}.",
                account_id=account_id,
                token=token,
                base_url=credentials.base_url,
                user_id=credentials.user_id,
            )
        return WeixinLoginResult(False, f"Unexpected Weixin login status: {status_name}")

    return WeixinLoginResult(False, "Weixin QR login timed out.")


def default_session_key() -> str:
    """Return a random login session key for callers that need one."""

    return uuid.uuid4().hex
