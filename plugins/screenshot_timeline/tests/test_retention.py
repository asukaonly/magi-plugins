"""Tests for retention maintenance."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType


def _load() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "retention.py"
    spec = importlib.util.spec_from_file_location("screenshot_timeline_retention", module_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_select_expired_captures_includes_only_past_due() -> None:
    mod = _load()
    metadata = {
        "media": {
            "captures": [
                {"capture_id": "a", "original_path": "/tmp/a.jpg", "original_expires_at": 100.0},
                {"capture_id": "b", "original_path": "/tmp/b.jpg", "original_expires_at": 200.0},
                {"capture_id": "c", "original_path": None, "original_expires_at": 50.0},
            ]
        }
    }
    expired = mod.select_expired(metadata, now=150.0)
    assert [c["capture_id"] for c in expired] == ["a"]


def test_delete_expired_files_removes_files_and_patches_metadata(tmp_path: Path) -> None:
    mod = _load()
    a = tmp_path / "a.jpg"
    a.write_bytes(b"x")
    b = tmp_path / "b.jpg"
    b.write_bytes(b"x")
    metadata = {
        "media": {
            "captures": [
                {"capture_id": "a", "original_path": str(a), "original_expires_at": 100.0,
                 "thumbnail_path": str(tmp_path / "a_thumb.jpg")},
                {"capture_id": "b", "original_path": str(b), "original_expires_at": 999.0,
                 "thumbnail_path": str(tmp_path / "b_thumb.jpg")},
            ]
        }
    }
    deleted_bytes = mod.purge_expired(metadata, now=150.0)
    assert deleted_bytes == 1
    assert not a.exists()
    assert b.exists()
    # patched
    assert metadata["media"]["captures"][0]["original_path"] is None
    assert metadata["media"]["captures"][1]["original_path"] == str(b)


def test_purge_is_idempotent(tmp_path: Path) -> None:
    mod = _load()
    metadata = {"media": {"captures": [
        {"capture_id": "a", "original_path": None, "original_expires_at": 1.0,
         "thumbnail_path": "/x"},
    ]}}
    assert mod.purge_expired(metadata, now=100.0) == 0
    assert metadata["media"]["captures"][0]["original_path"] is None
