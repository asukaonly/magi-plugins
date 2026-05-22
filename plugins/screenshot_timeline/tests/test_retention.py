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


import os as _os


def test_purge_orphan_originals_deletes_old_originals_only(tmp_path: Path) -> None:
    mod = _load()
    # Create date dirs with old/new originals + thumbnails
    old_dir = tmp_path / "2026" / "01" / "01"
    new_dir = tmp_path / "2026" / "05" / "20"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)

    old_orig = old_dir / "cap_ABC123_orig.jpg"
    old_thumb = old_dir / "cap_ABC123_thumb.jpg"
    new_orig = new_dir / "cap_DEF456_orig.jpg"
    new_thumb = new_dir / "cap_DEF456_thumb.jpg"

    for p in (old_orig, old_thumb, new_orig, new_thumb):
        p.write_bytes(b"x" * 100)

    # Backdate old files to 40 days ago; bring new files just before `now`
    forty_days = 40 * 86400.0
    now = 1_800_000_000.0
    old_mtime = now - forty_days
    new_mtime = now - 1.0  # well within retention window
    _os.utime(old_orig, (old_mtime, old_mtime))
    _os.utime(old_thumb, (old_mtime, old_mtime))
    _os.utime(new_orig, (new_mtime, new_mtime))
    _os.utime(new_thumb, (new_mtime, new_mtime))

    stats = mod.purge_orphan_originals(tmp_path, retention_days=30, now=now)

    assert stats["deleted"] == 1
    assert stats["deleted_bytes"] == 100
    assert stats["scanned"] >= 1
    assert not old_orig.exists()
    assert old_thumb.exists()    # thumbnails always kept
    assert new_orig.exists()     # not expired
    assert new_thumb.exists()


def test_purge_orphan_originals_handles_missing_root(tmp_path: Path) -> None:
    mod = _load()
    nonexistent = tmp_path / "does_not_exist"
    stats = mod.purge_orphan_originals(nonexistent, retention_days=30, now=1.0)
    assert stats == {"scanned": 0, "deleted": 0, "deleted_bytes": 0, "errors": 0}


def test_purge_orphan_originals_new_layout_originals_only(tmp_path: Path) -> None:
    """New layout: <root>/originals/YYYY/MM/DD/<capture_id>.jpg
    Everything under originals/ is fair game; thumbnails/ stays untouched."""
    mod = _load()
    old_orig_dir = tmp_path / "originals" / "2026" / "01" / "01"
    new_orig_dir = tmp_path / "originals" / "2026" / "05" / "20"
    old_thumb_dir = tmp_path / "thumbnails" / "2026" / "01" / "01"
    for d in (old_orig_dir, new_orig_dir, old_thumb_dir):
        d.mkdir(parents=True)

    old_orig = old_orig_dir / "20260101T120000_000000_AAAA.jpg"
    new_orig = new_orig_dir / "20260520T120000_000000_BBBB.jpg"
    old_thumb = old_thumb_dir / "20260101T120000_000000_AAAA.jpg"

    for p in (old_orig, new_orig, old_thumb):
        p.write_bytes(b"x" * 100)

    now = 1_800_000_000.0
    old_mtime = now - 40 * 86400.0
    new_mtime = now - 1.0
    _os.utime(old_orig, (old_mtime, old_mtime))
    _os.utime(old_thumb, (old_mtime, old_mtime))
    _os.utime(new_orig, (new_mtime, new_mtime))

    stats = mod.purge_orphan_originals(tmp_path, retention_days=30, now=now)

    assert stats["deleted"] == 1
    assert not old_orig.exists()
    assert new_orig.exists()         # within retention
    assert old_thumb.exists()        # thumbnails always kept


def test_purge_orphan_originals_handles_both_layouts_concurrently(tmp_path: Path) -> None:
    """During the 0.1.8 transition a single user can have legacy data
    alongside fresh new-layout data. Both must sweep correctly."""
    mod = _load()
    # Legacy old original
    legacy_dir = tmp_path / "2025" / "12" / "31"
    legacy_dir.mkdir(parents=True)
    legacy_orig = legacy_dir / "cap_OLDULID_orig.jpg"
    legacy_thumb = legacy_dir / "cap_OLDULID_thumb.jpg"
    legacy_orig.write_bytes(b"x" * 50)
    legacy_thumb.write_bytes(b"x" * 50)

    # New old original
    new_dir = tmp_path / "originals" / "2026" / "01" / "02"
    new_dir.mkdir(parents=True)
    new_orig = new_dir / "20260102T100000_000000_CCCC.jpg"
    new_orig.write_bytes(b"x" * 50)

    now = 1_800_000_000.0
    old_mtime = now - 60 * 86400.0
    for p in (legacy_orig, legacy_thumb, new_orig):
        _os.utime(p, (old_mtime, old_mtime))

    stats = mod.purge_orphan_originals(tmp_path, retention_days=30, now=now)

    assert stats["deleted"] == 2
    assert not legacy_orig.exists()
    assert not new_orig.exists()
    assert legacy_thumb.exists()     # legacy thumbnails preserved


def test_purge_orphan_originals_skips_unrelated_files(tmp_path: Path) -> None:
    mod = _load()
    # Files that should NOT be touched even if old
    other_jpg = tmp_path / "random.jpg"
    fake_orig = tmp_path / "screenshot_orig.jpg"  # doesn't match cap_*_orig.jpg
    legit_orig = tmp_path / "cap_XYZ789_orig.jpg"

    other_jpg.write_bytes(b"x")
    fake_orig.write_bytes(b"x")
    legit_orig.write_bytes(b"x")

    now = 1_800_000_000.0
    old = now - 60 * 86400.0
    for p in (other_jpg, fake_orig, legit_orig):
        _os.utime(p, (old, old))

    stats = mod.purge_orphan_originals(tmp_path, retention_days=30, now=now)

    assert stats["deleted"] == 1   # only legit_orig
    assert other_jpg.exists()
    assert fake_orig.exists()
    assert not legit_orig.exists()
