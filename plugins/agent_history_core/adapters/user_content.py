"""Separate leading runtime envelopes from authored transcript text."""
from __future__ import annotations

import re

_ENVELOPE = re.compile(
    r"^\s*<(?P<tag>environment_context|permissions instructions|skills_instructions|"
    r"collaboration_mode|external_codex_apps_[a-zA-Z0-9_]+)(?:\s[^>]*)?>",
)
_HANDBOOK = re.compile(r"^\s*# AGENTS\.md instructions for [^\n]+\s*\n+<INSTRUCTIONS>")


def authored_text(text: str) -> str:
    """Remove only recognized leading runtime blocks, preserving quotes and code.

    Once authored text begins, the rest is left untouched. In particular, a
    user discussing an XML tag or quoting a configuration is not filtered.
    """
    remaining = text
    while remaining.strip():
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
