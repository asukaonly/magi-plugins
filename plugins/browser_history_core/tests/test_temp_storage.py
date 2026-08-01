from __future__ import annotations

import os
from pathlib import Path
import stat
from types import SimpleNamespace
from typing import Any

import pytest

from browser_history_core import temp_storage
from browser_history_core.temp_storage import (
    ManagedDatabaseCopyStore,
    UnsafeTemporaryStoragePathError,
)


class _SimulatedReparseDirectory:
    def __init__(self, path: str) -> None:
        self._path = path
        self.parent = Path("/")
        self.removed = False

    def __fspath__(self) -> str:
        return self._path

    def lstat(self) -> Any:
        return SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=getattr(
                stat,
                "FILE_ATTRIBUTE_REPARSE_POINT",
                0x0400,
            ),
        )

    def rmdir(self) -> None:
        self.removed = True

    def unlink(self) -> None:
        raise AssertionError("Directory reparse points must use rmdir")


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


def test_root_reparse_directory_is_removed_without_scanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _SimulatedReparseDirectory("/simulated-root-junction")
    monkeypatch.setattr(Path, "is_junction", lambda _path: False, raising=False)
    monkeypatch.setattr(
        temp_storage.os,
        "scandir",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("Root reparse points must never be scanned")
        ),
    )

    temp_storage._clear_directory_contents_no_follow(root)

    assert root.removed is True


def test_ancestor_reparse_directory_is_rejected_without_scanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ancestor = _SimulatedReparseDirectory("/simulated-ancestor-junction")
    monkeypatch.setattr(Path, "is_junction", lambda _path: False, raising=False)
    monkeypatch.setattr(
        temp_storage,
        "_existing_components",
        lambda _path: [ancestor],
    )
    monkeypatch.setattr(
        temp_storage.os,
        "scandir",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("Ancestor reparse points must never be scanned")
        ),
    )

    with pytest.raises(UnsafeTemporaryStoragePathError, match="reparse point"):
        temp_storage._assert_parent_chain_without_symlinks(
            Path("/managed/root")
        )

    assert ancestor.removed is False


def test_child_reparse_directory_is_removed_without_scanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = _SimulatedReparseDirectory("/simulated-child-junction")
    monkeypatch.setattr(Path, "is_junction", lambda _path: False, raising=False)
    monkeypatch.setattr(
        temp_storage.os,
        "scandir",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("Child reparse points must never be scanned")
        ),
    )

    temp_storage._remove_entry_no_follow(child)

    assert child.removed is True
