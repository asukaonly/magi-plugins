"""Decode bounded Gmail MIME bodies without loading remote content."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from email.header import decode_header, make_header
from email.utils import getaddresses
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

from mail_core import MailMessage


class _TextHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        if not self.hidden and tag in {"p", "br", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _header_text(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except (ValueError, LookupError):
        return value


def normalize(raw: dict[str, Any], *, account_email: str = "") -> MailMessage:
    payload = raw.get("payload") or {}
    headers = {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in payload.get("headers", [])}
    plain, html, attachments = [], [], []
    complete = True
    stack = [payload]
    parts_seen = 0
    while stack:
        part = stack.pop()
        parts_seen += 1
        if parts_seen > 500:
            raise ValueError("Gmail MIME parts exceed the read budget")
        filename = str(part.get("filename") or "")
        if filename:
            attachments.append(_header_text(filename)[:1024])
            continue
        stack.extend(reversed(part.get("parts") or []))
        mime = str(part.get("mimeType") or "")
        if mime not in {"text/plain", "text/html"}:
            continue
        body = part.get("body") or {}
        encoded = str(body.get("data") or "")
        if not encoded and body.get("attachmentId"):
            complete = False
            continue
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        part_headers = {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in part.get("headers", [])}
        charset = re.search(r'charset\s*=\s*["\']?([^;"\'\s]+)', part_headers.get("content-type", ""), re.IGNORECASE)
        try:
            text = decoded.decode(charset.group(1) if charset else "utf-8", errors="replace")
        except LookupError:
            text, complete = decoded.decode("utf-8", errors="replace"), False
        if "\ufffd" in text:
            complete = False
        (plain if mime == "text/plain" else html).append(text)
    if plain:
        text = "\n".join(plain)
    else:
        parser = _TextHTML()
        parser.feed("\n".join(html))
        text = "".join(parser.parts)
    if not plain and not html and raw.get("snippet"):
        text, complete = str(raw["snippet"]), False
    text = text.replace("\x00", "").strip()
    recipients = [address for _, address in getaddresses([headers.get("to", ""), headers.get("cc", "")]) if address]
    if len(attachments) > 100 or len(raw.get("labelIds", [])) > 100:
        raise ValueError("Gmail metadata exceeds the read budget")
    version = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return MailMessage(object_id=str(raw["id"]), thread_id=str(raw["threadId"]), version=version,
        subject=_header_text(headers.get("subject", ""))[:1024], sender=_header_text(headers.get("from", ""))[:2048],
        recipients=recipients[:200], occurred_at=float(raw["internalDate"]) / 1000,
        body=text[:12000], labels=sorted(str(v) for v in raw.get("labelIds", []))[:100],
        attachment_names=attachments[:100], content_complete=complete and len(text) <= 12000 and len(recipients) <= 200,
        url=("https://mail.google.com/mail/?authuser=" + quote(account_email, safe="") + "#all/" + quote(str(raw["threadId"]), safe="")) if account_email else None)
