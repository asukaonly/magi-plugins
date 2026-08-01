"""Local user-content cleanup helpers for screenshot timeline storage.

The plugin writes sensitive material to two places: screenshot resources and
its private SQLite session database.  Cleanup is anchored to already-opened
directories and never follows symbolic links.  This keeps a crafted link from
turning a product data clear into deletion outside Magi-owned storage.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from pathlib import Path


_LEGACY_CAPTURE_FILE_RE = re.compile(
    r"^cap_[A-Z0-9]+_(?:orig|thumb)\.jpg$",
    re.IGNORECASE,
)
_SESSION_SIDECAR_SUFFIXES = ("", "-wal", "-shm", "-journal")
_ZERO_CHUNK = b"\0" * (1024 * 1024)


class UnsafeStoragePathError(RuntimeError):
    """Raised when a managed storage path crosses a symbolic link."""


class StorageCleanupError(RuntimeError):
    """Raised when managed user content cannot be removed completely."""


def erase_session_database(db_path: Path) -> None:
    """Best-effort overwrite and remove a session DB plus SQLite sidecars.

    The overwrite reduces ordinary undelete exposure, but intentionally makes
    no physical-erasure guarantee for copy-on-write filesystems, SSD firmware,
    backups, or snapshots.  Symbolic links are unlinked without touching their
    targets.  A symbolic-link ancestor is rejected because it would move the
    entire cleanup boundary outside the configured directory.
    """

    path = _absolute_path(db_path)
    if not path.name or path.parent == path:
        raise UnsafeStoragePathError(f"Invalid session database path: {path}")

    parent_fd = _open_directory_without_symlinks(path.parent, missing_ok=True)
    if parent_fd is None:
        return
    try:
        for suffix in _SESSION_SIDECAR_SUFFIXES:
            _erase_database_entry(parent_fd, f"{path.name}{suffix}")
        _verify_open_directory(path.parent, parent_fd)
        for suffix in _SESSION_SIDECAR_SUFFIXES:
            if _lstat_optional(parent_fd, f"{path.name}{suffix}") is not None:
                raise StorageCleanupError(
                    f"Session database artifact still exists: {path.name}{suffix}"
                )
    finally:
        os.close(parent_fd)


def erase_managed_screenshot_resources(resources_root: Path) -> dict[str, int]:
    """Remove current and legacy screenshot files without leaving the root.

    New-layout ``originals`` and ``thumbnails`` trees are entirely plugin-owned
    and are removed recursively.  Outside those trees, only legacy capture
    filenames are removed so unrelated files under the resource root survive.
    Internal symbolic links are unlinked but never traversed.
    """

    root = _absolute_path(resources_root)
    if root.parent == root:
        raise UnsafeStoragePathError("Refusing to clear the filesystem root")

    root_fd = _open_directory_without_symlinks(root, missing_ok=True)
    stats = {
        "deleted_files": 0,
        "deleted_bytes": 0,
        "deleted_symlinks": 0,
        "deleted_special_entries": 0,
    }
    if root_fd is None:
        return stats
    try:
        for subtree_name in ("originals", "thumbnails"):
            _remove_managed_tree_entry(root_fd, subtree_name, stats)
        _remove_legacy_capture_files(root_fd, stats, is_root=True)
        _verify_open_directory(root, root_fd)
        for subtree_name in ("originals", "thumbnails"):
            if _lstat_optional(root_fd, subtree_name) is not None:
                raise StorageCleanupError(
                    f"Managed screenshot subtree still exists: {subtree_name}"
                )
        if _contains_legacy_capture_files(root_fd, is_root=True):
            raise StorageCleanupError("Legacy screenshot files remain after cleanup")
    finally:
        os.close(root_fd)
    return stats


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _directory_open_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if os.name != "posix" or any(not hasattr(os, name) for name in required):
        raise StorageCleanupError(
            "Symlink-safe screenshot cleanup requires POSIX directory handles"
        )
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_directory_without_symlinks(
    path: Path,
    *,
    missing_ok: bool,
) -> int | None:
    """Open every absolute path component with ``O_NOFOLLOW``."""

    absolute = _absolute_path(path)
    flags = _directory_open_flags()
    parts = absolute.parts
    if not parts or parts[0] != os.sep:
        raise UnsafeStoragePathError(f"Storage path is not absolute: {absolute}")

    current_fd = os.open(os.sep, flags)
    try:
        for component in parts[1:]:
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if missing_ok:
                    os.close(current_fd)
                    return None
                raise
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise UnsafeStoragePathError(
                        f"Storage path crosses a symbolic link: {absolute}"
                    ) from exc
                raise StorageCleanupError(
                    f"Cannot open managed storage directory: {absolute}"
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _verify_open_directory(path: Path, expected_fd: int) -> None:
    reopened_fd = _open_directory_without_symlinks(path, missing_ok=False)
    assert reopened_fd is not None
    try:
        expected = os.fstat(expected_fd)
        actual = os.fstat(reopened_fd)
        if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            raise UnsafeStoragePathError(
                f"Managed storage directory changed during cleanup: {path}"
            )
    finally:
        os.close(reopened_fd)


def _lstat_optional(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_child_directory(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
) -> int:
    try:
        child_fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.ENOENT}:
            raise UnsafeStoragePathError(
                f"Managed directory changed during cleanup: {name}"
            ) from exc
        raise StorageCleanupError(f"Cannot open managed directory: {name}") from exc
    actual = os.fstat(child_fd)
    if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
        os.close(child_fd)
        raise UnsafeStoragePathError(
            f"Managed directory changed during cleanup: {name}"
        )
    return child_fd


def _erase_database_entry(parent_fd: int, name: str) -> None:
    entry_stat = _lstat_optional(parent_fd, name)
    if entry_stat is None:
        return
    mode = entry_stat.st_mode
    if stat.S_ISLNK(mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    if stat.S_ISDIR(mode):
        raise StorageCleanupError(
            f"Refusing to remove directory at session database path: {name}"
        )
    if not stat.S_ISREG(mode):
        os.unlink(name, dir_fd=parent_fd)
        return

    # Do not overwrite a multiply-linked inode: that would modify content
    # reachable outside the managed path.  Removing this directory entry is
    # sufficient to sever the plugin's access to it.
    if entry_stat.st_nlink == 1 and entry_stat.st_size > 0:
        flags = (
            os.O_WRONLY
            | os.O_NOFOLLOW
            | os.O_NONBLOCK
            | getattr(os, "O_CLOEXEC", 0)
        )
        file_fd = os.open(name, flags, dir_fd=parent_fd)
        try:
            opened = os.fstat(file_fd)
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (entry_stat.st_dev, entry_stat.st_ino):
                raise UnsafeStoragePathError(
                    f"Session database artifact changed during cleanup: {name}"
                )
            remaining = opened.st_size
            os.lseek(file_fd, 0, os.SEEK_SET)
            while remaining > 0:
                chunk = _ZERO_CHUNK[: min(remaining, len(_ZERO_CHUNK))]
                written = os.write(file_fd, chunk)
                if written <= 0:
                    raise StorageCleanupError(
                        f"Could not overwrite session database artifact: {name}"
                    )
                remaining -= written
            os.fsync(file_fd)
        finally:
            os.close(file_fd)

    current = _lstat_optional(parent_fd, name)
    if current is None:
        return
    if (current.st_dev, current.st_ino) != (entry_stat.st_dev, entry_stat.st_ino):
        raise UnsafeStoragePathError(
            f"Session database artifact changed during cleanup: {name}"
        )
    os.unlink(name, dir_fd=parent_fd)


def _remove_managed_tree_entry(
    parent_fd: int,
    name: str,
    stats: dict[str, int],
) -> None:
    entry_stat = _lstat_optional(parent_fd, name)
    if entry_stat is None:
        return
    mode = entry_stat.st_mode
    if stat.S_ISLNK(mode):
        os.unlink(name, dir_fd=parent_fd)
        stats["deleted_symlinks"] += 1
        return
    if stat.S_ISREG(mode):
        os.unlink(name, dir_fd=parent_fd)
        stats["deleted_files"] += 1
        stats["deleted_bytes"] += entry_stat.st_size
        return
    if not stat.S_ISDIR(mode):
        os.unlink(name, dir_fd=parent_fd)
        stats["deleted_special_entries"] += 1
        return

    child_fd = _open_child_directory(parent_fd, name, entry_stat)
    try:
        _remove_managed_tree_contents(child_fd, stats)
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _remove_managed_tree_contents(
    directory_fd: int,
    stats: dict[str, int],
) -> None:
    with os.scandir(directory_fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        _remove_managed_tree_entry(directory_fd, name, stats)


def _remove_legacy_capture_files(
    directory_fd: int,
    stats: dict[str, int],
    *,
    is_root: bool,
) -> None:
    with os.scandir(directory_fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        if is_root and name in {"originals", "thumbnails"}:
            continue
        entry_stat = _lstat_optional(directory_fd, name)
        if entry_stat is None:
            continue
        mode = entry_stat.st_mode
        if stat.S_ISLNK(mode):
            if _LEGACY_CAPTURE_FILE_RE.match(name):
                os.unlink(name, dir_fd=directory_fd)
                stats["deleted_symlinks"] += 1
            continue
        if stat.S_ISDIR(mode):
            child_fd = _open_child_directory(directory_fd, name, entry_stat)
            try:
                _remove_legacy_capture_files(child_fd, stats, is_root=False)
            finally:
                os.close(child_fd)
            continue
        if not _LEGACY_CAPTURE_FILE_RE.match(name):
            continue
        os.unlink(name, dir_fd=directory_fd)
        if stat.S_ISREG(mode):
            stats["deleted_files"] += 1
            stats["deleted_bytes"] += entry_stat.st_size
        else:
            stats["deleted_special_entries"] += 1


def _contains_legacy_capture_files(
    directory_fd: int,
    *,
    is_root: bool,
) -> bool:
    with os.scandir(directory_fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        if is_root and name in {"originals", "thumbnails"}:
            continue
        entry_stat = _lstat_optional(directory_fd, name)
        if entry_stat is None:
            continue
        mode = entry_stat.st_mode
        if stat.S_ISDIR(mode):
            child_fd = _open_child_directory(directory_fd, name, entry_stat)
            try:
                if _contains_legacy_capture_files(child_fd, is_root=False):
                    return True
            finally:
                os.close(child_fd)
        elif _LEGACY_CAPTURE_FILE_RE.match(name):
            return True
    return False


__all__ = [
    "StorageCleanupError",
    "UnsafeStoragePathError",
    "erase_managed_screenshot_resources",
    "erase_session_database",
]
