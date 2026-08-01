from __future__ import annotations

import os
from pathlib import Path

import pytest

from browser_history_core.temp_storage import (
    ManagedDatabaseCopyStore,
    UnsafeTemporaryStoragePathError,
)


def test_prepare_sweeps_crash_residuals_without_following_internal_symlinks(
    tmp_path: Path,
) -> None:
    temp_root = tmp_path / "database-copies"
    crashed_copy = temp_root / "copy-crashed"
    crashed_copy.mkdir(parents=True)
    (crashed_copy / "History").write_bytes(b"private browser history")
    external_file = tmp_path / "external.db"
    external_file.write_bytes(b"must survive")
    (crashed_copy / "external-link").symlink_to(external_file)

    store = ManagedDatabaseCopyStore(namespace="test-browser", temp_root=temp_root)
    prepared_root = store.prepare()

    assert prepared_root == temp_root
    assert list(temp_root.iterdir()) == []
    assert external_file.read_bytes() == b"must survive"


def test_root_symlink_is_replaced_without_touching_its_target(tmp_path: Path) -> None:
    external_directory = tmp_path / "external"
    external_directory.mkdir()
    external_file = external_directory / "private.db"
    external_file.write_bytes(b"must survive")
    temp_root = tmp_path / "database-copies"
    temp_root.symlink_to(external_directory, target_is_directory=True)

    store = ManagedDatabaseCopyStore(namespace="test-browser", temp_root=temp_root)
    store.prepare()

    assert temp_root.is_dir()
    assert temp_root.is_symlink() is False
    assert external_file.read_bytes() == b"must survive"


def test_prepare_rejects_a_symlinked_ancestor(tmp_path: Path) -> None:
    external_directory = tmp_path / "external"
    external_directory.mkdir()
    external_file = external_directory / "private.db"
    external_file.write_bytes(b"must survive")
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(external_directory, target_is_directory=True)
    temp_root = linked_parent / "database-copies"

    store = ManagedDatabaseCopyStore(namespace="test-browser", temp_root=temp_root)

    with pytest.raises(UnsafeTemporaryStoragePathError, match="ancestor"):
        store.prepare()

    assert external_file.read_bytes() == b"must survive"
    assert not (external_directory / "database-copies").exists()


def test_copy_directories_are_created_and_removed_inside_the_namespace(
    tmp_path: Path,
) -> None:
    temp_root = tmp_path / "database-copies"
    store = ManagedDatabaseCopyStore(namespace="test-browser", temp_root=temp_root)

    copy_dir = store.create_copy_dir()
    (copy_dir / "History").write_bytes(b"temporary copy")

    assert copy_dir.parent == temp_root
    assert os.path.commonpath((copy_dir, temp_root)) == str(temp_root)
    store.cleanup_copy(copy_dir)
    assert copy_dir.exists() is False

    (temp_root / "copy-crashed").mkdir()
    store.clear()
    assert list(temp_root.iterdir()) == []
