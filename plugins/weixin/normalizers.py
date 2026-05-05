"""Normalize Weixin protocol messages into Magi channel text."""

from __future__ import annotations

from typing import Any

from .api import MESSAGE_ITEM_FILE, MESSAGE_ITEM_IMAGE, MESSAGE_ITEM_TEXT, MESSAGE_ITEM_VIDEO, MESSAGE_ITEM_VOICE


def body_from_item_list(item_list: list[dict[str, Any]] | None) -> str:
    """Extract user-visible text from Weixin message items."""

    if not item_list:
        return ""
    parts: list[str] = []
    for item in item_list:
        item_type = item.get("type")
        if item_type == MESSAGE_ITEM_TEXT:
            text_item = item.get("text_item")
            text = str(text_item.get("text") or "") if isinstance(text_item, dict) else ""
            ref = item.get("ref_msg")
            if isinstance(ref, dict):
                text = _with_reference(text, ref)
            if text.strip():
                parts.append(text.strip())
        if item_type == MESSAGE_ITEM_VOICE:
            voice_item = item.get("voice_item")
            if isinstance(voice_item, dict) and voice_item.get("text"):
                parts.append(f"Voice transcript: {voice_item.get('text')}")
            else:
                parts.append("[Voice message attached]")
        if item_type == MESSAGE_ITEM_IMAGE:
            parts.append("[Image attached]")
        if item_type == MESSAGE_ITEM_FILE:
            file_item = item.get("file_item")
            file_name = str(file_item.get("file_name") or "file") if isinstance(file_item, dict) else "file"
            parts.append(f"[File attached: {file_name}]")
        if item_type == MESSAGE_ITEM_VIDEO:
            parts.append("[Video attached]")
    return "\n".join(parts)


def _with_reference(text: str, ref: dict[str, Any]) -> str:
    parts: list[str] = []
    title = ref.get("title")
    if title:
        parts.append(str(title))
    message_item = ref.get("message_item")
    if isinstance(message_item, dict):
        ref_body = body_from_item_list([message_item])
        if ref_body:
            parts.append(ref_body)
    if not parts:
        return text
    return f"[Quoted: {' | '.join(parts)}]\n{text}"
