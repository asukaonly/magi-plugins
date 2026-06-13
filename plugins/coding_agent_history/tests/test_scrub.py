"""Tests for the secret-scrubbing util.

Loads ``scrub.py`` via the repo's synthesized-loader convention (mirrors
screenshot_timeline/tests/test_ids.py) rather than a sys.path import, so it
exercises the worktree copy directly.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_scrub() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "scrub.py"
    spec = importlib.util.spec_from_file_location("coding_agent_history_scrub", module_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # required for @dataclass / from __future__ under 3.12
    spec.loader.exec_module(mod)
    return mod


redact_secrets = _load_scrub().redact_secrets


def test_redacts_common_secret_classes() -> None:
    cases = [
        "my key is sk-ABCDEF0123456789ABCDEF0123456789 ok",           # OpenAI-style
        "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 done",        # GitHub PAT
        "aws AKIAIOSFODNN7EXAMPLE here",                              # AWS access key id
        "google AIzaSyA1234567890_abcDEFghiJKLmnoPQRstuv here",       # Google API key
        "DATABASE_PASSWORD=supersecretvalue123",                      # .env assignment
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIn.eyJzdWIiOiIx.abc",  # JWT / bearer
    ]
    for c in cases:
        out = redact_secrets(c)
        assert "[REDACTED" in out, c
        # the raw secret token must be gone
        for tok in (
            "sk-ABCDEF0123456789",
            "ghp_ABCDEFGHIJ",
            "AKIAIOSFODNN7EXAMPLE",
            "AIzaSyA1234567890",
            "supersecretvalue123",
            "eyJhbGciOiJIUzI1NiIn",
        ):
            assert tok not in out


def test_preserves_ordinary_prose() -> None:
    text = "I want to refactor the auth module and add a parser test."
    assert redact_secrets(text) == text


def test_redacts_private_key_block() -> None:
    block = (
        "before\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "abc\ndef\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
        "after"
    )
    out = redact_secrets(block)
    assert "BEGIN OPENSSH PRIVATE KEY" not in out and "[REDACTED:private_key]" in out
    assert "before" in out and "after" in out
