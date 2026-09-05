"""Tests for symlink-safe screenshot and session storage cleanup."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import socket
import sqlite3
import tempfile

import pytest

from screenshot_timeline import storage_cleanup
from screenshot_timeline.storage_cleanup import (
    UnsafeStoragePathError,
    erase_managed_screenshot_resources,
    erase_session_database,
)


def test_erase_managed_resources_covers_private_trees_and_internal_symlinks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "screenshots"
    original = root / "originals" / "2026" / "08" / "01" / "capture.jpg"
    thumbnail = root / "thumbnails" / "2026" / "08" / "01" / "capture.jpg"
    unrelated = root / "keep.txt"
    for path in (original, thumbnail, unrelated):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.name.encode())

    external = tmp_path / "external"
    external.mkdir()
    external_capture = external / "outside.jpg"
    external_capture.write_bytes(b"outside")
    (root / "originals" / "linked-outside").symlink_to(
        external,
        target_is_directory=True,
    )

    stats = erase_managed_screenshot_resources(root)

    assert stats["deleted_files"] == 2
    assert stats["deleted_symlinks"] == 1
    assert not (root / "originals").exists()
    assert not (root / "thumbnails").exists()
    assert unrelated.read_bytes() == b"keep.txt"
    assert external_capture.read_bytes() == b"outside"


def test_erase_managed_resources_rejects_symlinked_root_ancestor(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external"
    target_root = external / "screenshots"
    capture = target_root / "originals" / "2026" / "08" / "01" / "capture.jpg"
    capture.parent.mkdir(parents=True)
    capture.write_bytes(b"outside")
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(external, target_is_directory=True)

    with pytest.raises(UnsafeStoragePathError):
        erase_managed_screenshot_resources(linked_parent / "screenshots")

    assert capture.read_bytes() == b"outside"


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "mkfifo"),
    reason="POSIX FIFOs are unavailable",
)
def test_erase_managed_resources_unlinks_fifo_entries(
    tmp_path: Path,
) -> None:
    root = tmp_path / "screenshots"
    originals = root / "originals"
    originals.mkdir(parents=True)
    fifo_path = originals / "capture.fifo"
    os.mkfifo(fifo_path)

    stats = erase_managed_screenshot_resources(root)

    assert not originals.exists()
    assert stats["deleted_special_entries"] == 1


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(socket, "AF_UNIX"),
    reason="POSIX sockets are unavailable",
)
def test_erase_managed_resources_unlinks_socket_entries() -> None:
    short_temp_root = Path("/private/tmp")
    if not short_temp_root.is_dir():
        short_temp_root = Path("/tmp").resolve()
    with tempfile.TemporaryDirectory(
        prefix="magi-shot-",
        dir=short_temp_root,
    ) as temp_dir:
        root = Path(temp_dir) / "screenshots"
        originals = root / "originals"
        originals.mkdir(parents=True)
        socket_path = originals / "capture.sock"
        unix_socket = socket.socket(socket.AF_UNIX)
        try:
            try:
                unix_socket.bind(os.fspath(socket_path))
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EPERM}:
                    pytest.skip("The test environment blocks Unix socket binding")
                raise
        finally:
            unix_socket.close()

        stats = erase_managed_screenshot_resources(root)

        assert not originals.exists()
        assert stats["deleted_special_entries"] == 1


def test_erase_session_database_removes_database_and_sidecars(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE sessions (session_id TEXT)")
    connection.execute("INSERT INTO sessions VALUES ('private-session')")
    connection.commit()
    connection.close()

    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{db_path}{suffix}").write_bytes(f"private{suffix}".encode())

    external = tmp_path / "external-sidecar"
    external.write_bytes(b"outside")
    Path(f"{db_path}-shm").unlink()
    Path(f"{db_path}-shm").symlink_to(external)

    erase_session_database(db_path)

    for suffix in ("", "-wal", "-shm", "-journal"):
        assert not os.path.lexists(f"{db_path}{suffix}")
    assert external.read_bytes() == b"outside"


def test_erase_session_database_rejects_symlinked_parent(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    db_path = external / "sessions.db"
    db_path.write_bytes(b"private")
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(external, target_is_directory=True)

    with pytest.raises(UnsafeStoragePathError):
        erase_session_database(linked_parent / "sessions.db")

    assert db_path.read_bytes() == b"private"


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "mkfifo"),
    reason="POSIX FIFOs are unavailable",
)
def test_erase_session_database_unlinks_fifo_without_opening_it(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sessions.db"
    os.mkfifo(db_path)

    erase_session_database(db_path)

    assert not os.path.lexists(db_path)


def test_erase_session_database_opens_regular_files_nonblocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "sessions.db"
    db_path.write_bytes(b"private")
    real_open = storage_cleanup.os.open
    database_open_flags: list[int] = []

    def tracking_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == db_path.name:
            database_open_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(storage_cleanup.os, "open", tracking_open)

    erase_session_database(db_path)

    assert database_open_flags
    assert all(flags & os.O_NONBLOCK for flags in database_open_flags)
