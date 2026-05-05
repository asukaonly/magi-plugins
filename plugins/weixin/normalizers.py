"""Normalize Weixin protocol messages into Magi channel text."""

from __future__ import annotations

from typing import Any

from .api import MESSAGE_ITEM_TEXT, MESSAGE_ITEM_VOICE


def body_from_item_list(item_list: list[dict[str, Any]] | None) -> str:
    """Extract user-visible text from Weixin message items."""

    if not item_list:
        return ""
    for item in item_list:
        item_type = item.get("type")
        if item_type == MESSAGE_ITEM_TEXT:
            text_item = item.get("text_item")
            text = str(text_item.get("text") or "") if isinstance(text_item, dict) else ""
            ref = item.get("ref_msg")
            if isinstance(ref, dict):
                return _with_reference(text, ref)
            return text
        if item_type == MESSAGE_ITEM_VOICE:
            voice_item = item.get("voice_item")
            if isinstance(voice_item, dict) and voice_item.get("text"):
                return str(voice_item.get("text") or "")
    return ""


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
