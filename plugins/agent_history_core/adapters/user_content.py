"""Separate leading runtime envelopes from authored transcript text."""
from __future__ import annotations

import json
import re

_ENVELOPE = re.compile(
    r"^\s*<(?P<tag>environment_context|permissions instructions|skills_instructions|"
    r"collaboration_mode|codex_internal_context|in-app-browser-context|"
    r"external_codex_apps_[a-zA-Z0-9_]+)(?:\s[^>]*)?>",
)
_HANDBOOK = re.compile(r"^\s*# AGENTS\.md instructions for [^\n]+\s*\n+<INSTRUCTIONS>")
_QUESTION_REPLY = re.compile(
    r"^\s*<send_user_message_question_reply>(.*?)</send_user_message_question_reply>\s*$",
    re.DOTALL,
)


def authored_text(text: str) -> str:
    """Remove only recognized leading runtime blocks, preserving quotes and code.

    Once authored text begins, the rest is left untouched. In particular, a
    user discussing an XML tag or quoting a configuration is not filtered.
    """
    remaining = text
    while remaining.strip():
        reply = _QUESTION_REPLY.fullmatch(remaining)
        if reply:
            return _question_reply_text(reply.group(1))
        handbook = _HANDBOOK.match(remaining)
        envelope = _ENVELOPE.match(remaining)
        if handbook:
            tag, end = "INSTRUCTIONS", handbook.end()
        elif envelope:
            tag, end = envelope.group("tag"), envelope.end()
        else:
            return remaining if remaining.strip() else ""
        closing = remaining.find(f"</{tag}>", end)
        if closing < 0:
            return ""
        remaining = remaining[closing + len(tag) + 3:].lstrip("\r\n")
    return ""


def _question_reply_text(payload: str) -> str:
    """Keep explicit answers without attributing the assistant's question to the user."""
    try:
        items = json.loads(payload)
    except (ValueError, TypeError):
        return ""
    if not isinstance(items, list):
        return ""
    replies = []
    for item in items:
        if not isinstance(item, dict):
            continue
        answer = item.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            continue
        question = item.get("question")
        context = (
            f"Assistant question (context only, not a user statement):\n{question}\n\n"
            if isinstance(question, str) and question.strip() else ""
        )
        replies.append(f"{context}User answer:\n{answer}")
    return "\n\n".join(replies)
