"""Best-effort secret redaction for ingested coding-agent transcripts.

Bias: over-redact secrets, never touch ordinary prose. Runs before any content
leaves the sensor (the memory pipeline does no redaction and uploads content to
the configured LLM).
"""
from __future__ import annotations

import re

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
# (label, pattern) — order matters (private key handled separately above).
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # JWT: anchored on the base64url ``eyJ`` header + the 2-dot structure, so the
    # trailing signature floor can stay small without risk of matching prose.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{2,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google_key", re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    # .env / inline secret assignment: KEY=value where KEY hints secrecy.
    (
        "env_secret",
        re.compile(
            r"\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY)[A-Z0-9_]*)\s*[:=]\s*\S+",
            re.IGNORECASE,
        ),
    ),
    # Long high-entropy hex / base64 blobs (length floor avoids normal words).
    ("hex_blob", re.compile(r"\b[0-9a-fA-F]{40,}\b")),
    ("base64_blob", re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")),
]


def redact_secrets(text: str) -> str:
    """Return ``text`` with obvious secret tokens replaced by ``[REDACTED:<label>]``."""
    if not text:
        return text
    out = _PRIVATE_KEY_RE.sub("[REDACTED:private_key]", text)
    for label, pat in _PATTERNS:
        if label == "env_secret":
            out = pat.sub(lambda m: f"{m.group(1)}=[REDACTED:env_secret]", out)
        else:
            out = pat.sub(f"[REDACTED:{label}]", out)
    return out
