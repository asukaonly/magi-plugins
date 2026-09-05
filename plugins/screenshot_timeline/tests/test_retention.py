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


import os as _os


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


def test_purge_orphan_originals_skips_unrelated_files(tmp_path: Path) -> None:
    mod = _load()
    # Files that should NOT be touched even if old
    other_jpg = tmp_path / "random.jpg"
    fake_orig = tmp_path / "screenshot_orig.jpg"
    legit_orig = tmp_path / "originals" / "capture.jpg"
    legit_orig.parent.mkdir()

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
