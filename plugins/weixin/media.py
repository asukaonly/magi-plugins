"""Download and normalize Weixin inbound media attachments."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from magi_plugin_sdk.channels import ChannelAttachmentStoreProtocol

from .api import (
    DEFAULT_CDN_BASE_URL,
    MESSAGE_ITEM_FILE,
    MESSAGE_ITEM_IMAGE,
    MESSAGE_ITEM_VIDEO,
    MESSAGE_ITEM_VOICE,
    WeixinApiError,
)


WEIXIN_MEDIA_MAX_BYTES = 100 * 1024 * 1024


@dataclass(slots=True)
class WeixinMediaResult:
    attachments: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def collect_media_attachments(
    item_list: list[dict[str, Any]],
    *,
    attachment_store: ChannelAttachmentStoreProtocol,
    cdn_base_url: str,
    session_id: str,
    turn_id: str,
    message_key: str,
) -> WeixinMediaResult:
    result = WeixinMediaResult()
    for index, item in enumerate(item_list):
        if not isinstance(item, dict):
            continue
        try:
            attachment = await _attachment_from_item(
                item,
                attachment_store=attachment_store,
                cdn_base_url=cdn_base_url,
                session_id=session_id,
                turn_id=turn_id,
                message_key=message_key,
                index=index,
            )
        except Exception as exc:
            result.errors.append(str(exc))
            continue
        if attachment:
            if str(attachment.get("mime_type") or "").startswith(("audio/", "video/")):
                attachment["parse_status"] = "unsupported"
                result.notes.append(f"Stored {attachment.get('mime_type')} attachment for manual review.")
            result.attachments.append(attachment)
    return result


async def _attachment_from_item(
    item: dict[str, Any],
    *,
    attachment_store: ChannelAttachmentStoreProtocol,
    cdn_base_url: str,
    session_id: str,
    turn_id: str,
    message_key: str,
    index: int,
) -> dict[str, Any] | None:
    item_type = item.get("type")
    if item_type == MESSAGE_ITEM_IMAGE:
        image_item = item.get("image_item")
        if not isinstance(image_item, dict):
            return None
        media = _media_dict(image_item)
        content = await _download_item_media(media, cdn_base_url=cdn_base_url, aes_key=_image_aes_key(image_item, media))
        mime_type = _detect_mime(content, default="image/jpeg")
        return await attachment_store.store_attachment(
            session_id=session_id,
            turn_id=turn_id,
            kind="image",
            original_name=f"weixin-image-{message_key}-{index}{_extension_for_mime(mime_type, '.jpg')}",
            content=content,
            mime_type=mime_type,
        )
    if item_type == MESSAGE_ITEM_VOICE:
        voice_item = item.get("voice_item")
        if not isinstance(voice_item, dict):
            return None
        media = _media_dict(voice_item)
        content = await _download_item_media(media, cdn_base_url=cdn_base_url, aes_key=str(media.get("aes_key") or ""))
        return await attachment_store.store_attachment(
            session_id=session_id,
            turn_id=turn_id,
            kind="file",
            original_name=f"weixin-voice-{message_key}-{index}.silk",
            content=content,
            mime_type="audio/silk",
        )
    if item_type == MESSAGE_ITEM_FILE:
        file_item = item.get("file_item")
        if not isinstance(file_item, dict):
            return None
        media = _media_dict(file_item)
        file_name = str(file_item.get("file_name") or f"weixin-file-{message_key}-{index}.bin")
        content = await _download_item_media(media, cdn_base_url=cdn_base_url, aes_key=str(media.get("aes_key") or ""))
        return await attachment_store.store_attachment(
            session_id=session_id,
            turn_id=turn_id,
            kind="file",
            original_name=file_name,
            content=content,
            mime_type=mimetypes.guess_type(file_name)[0] or "application/octet-stream",
        )
    if item_type == MESSAGE_ITEM_VIDEO:
        video_item = item.get("video_item")
        if not isinstance(video_item, dict):
            return None
        media = _media_dict(video_item)
        content = await _download_item_media(media, cdn_base_url=cdn_base_url, aes_key=str(media.get("aes_key") or ""))
        return await attachment_store.store_attachment(
            session_id=session_id,
            turn_id=turn_id,
            kind="file",
            original_name=f"weixin-video-{message_key}-{index}.mp4",
            content=content,
            mime_type="video/mp4",
        )
    return None


def _media_dict(item: dict[str, Any]) -> dict[str, Any]:
    media = item.get("media")
    return media if isinstance(media, dict) else {}


def _image_aes_key(image_item: dict[str, Any], media: dict[str, Any]) -> str:
    raw_hex = str(image_item.get("aeskey") or "").strip()
    if raw_hex:
        return base64.b64encode(bytes.fromhex(raw_hex)).decode("ascii")
    return str(media.get("aes_key") or "")


async def _download_item_media(media: dict[str, Any], *, cdn_base_url: str, aes_key: str) -> bytes:
    encrypted_query_param = str(media.get("encrypt_query_param") or "")
    full_url = str(media.get("full_url") or "")
    if not encrypted_query_param and not full_url:
        raise WeixinApiError("Weixin media item is missing CDN download parameters")
    raw = await asyncio.to_thread(_download_media_bytes, encrypted_query_param, cdn_base_url, full_url)
    if len(raw) > WEIXIN_MEDIA_MAX_BYTES:
        raise WeixinApiError("Weixin media item exceeds the maximum supported size")
    if not aes_key:
        return raw
    return _decrypt_aes_ecb(raw, _parse_aes_key(aes_key))


def _download_media_bytes(encrypted_query_param: str, cdn_base_url: str, full_url: str = "") -> bytes:
    url = full_url or _build_cdn_download_url(encrypted_query_param, cdn_base_url)
    request = urllib.request.Request(url, headers={"User-Agent": "Magi-Weixin/0.2"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise WeixinApiError(f"Weixin CDN HTTP {exc.code}: {detail}") from exc


def _build_cdn_download_url(encrypted_query_param: str, cdn_base_url: str) -> str:
    base_url = (cdn_base_url or DEFAULT_CDN_BASE_URL).rstrip("/")
    return f"{base_url}/download?encrypted_query_param={urllib.parse.quote(encrypted_query_param)}"


def _parse_aes_key(aes_key_base64: str) -> bytes:
    decoded = base64.b64decode(aes_key_base64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        candidate = decoded.decode("ascii", errors="ignore")
        if all(char in "0123456789abcdefABCDEF" for char in candidate):
            return bytes.fromhex(candidate)
    raise WeixinApiError("Weixin media aes_key must decode to 16 raw bytes or a 32-character hex key")


def _decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise RuntimeError("Weixin media support requires the 'cryptography' package.") from exc

    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def _detect_mime(content: bytes, *, default: str) -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"GIF87a") or content.startswith(b"GIF89a"):
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return default


def _extension_for_mime(mime_type: str, fallback: str) -> str:
    return mimetypes.guess_extension(mime_type) or fallback