"""Crash-recoverable temporary database-copy storage for local readers."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


class UnsafeTemporaryStoragePathError(RuntimeError):
    """Raised when a temporary namespace crosses a symbolic link."""


class ManagedDatabaseCopyStore:
    """Own one fixed namespace for disposable local database copies."""

    def __init__(self, *, namespace: str, temp_root: Path | None = None) -> None:
        normalized = "".join(
            character if character.isalnum() or character in {"-", "_"} else "-"
            for character in str(namespace or "").strip().lower()
        ).strip("-")
        if not normalized:
            raise ValueError("Temporary database namespace must not be empty")
        self._default_root = (
            _absolute(temp_root)
            if temp_root is not None
            else _absolute(
                Path(os.path.realpath(tempfile.gettempdir()))
                / "magi-plugin-temp"
                / "database-copies"
                / normalized
            )
        )
        self._prepared_root: Path | None = None

    @property
    def root(self) -> Path:
        return self._prepared_root or self._default_root

    def prepare(self, temp_root: Path | None = None) -> Path:
        """Sweep crash leftovers once, then create a trusted real directory."""

        root = _absolute(temp_root) if temp_root is not None else self._default_root
        if self._prepared_root != root:
            _clear_directory_contents_no_follow(root)
            _create_directory_without_symlinks(root)
            self._prepared_root = root
        else:
            _assert_directory_without_symlinks(root)
        return root

    def create_copy_dir(self) -> Path:
        root = self.prepare(self.root)
        return Path(tempfile.mkdtemp(prefix="copy-", dir=root))

    def cleanup_copy(self, copy_dir: Path) -> None:
        root = self.root
        candidate = _absolute(copy_dir)
        if candidate.parent != root:
            raise UnsafeTemporaryStoragePathError(
                f"Temporary copy is outside its managed namespace: {candidate}"
            )
        _remove_entry_no_follow(candidate)

    def clear(self, temp_root: Path | None = None) -> None:
        root = _absolute(temp_root) if temp_root is not None else self.root
        _clear_directory_contents_no_follow(root)
        if self._prepared_root == root:
            self._prepared_root = None


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_exists_no_follow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _existing_components(path: Path) -> list[Path]:
    absolute = _absolute(path)
    components: list[Path] = []
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        components.append(current)
    return components


def _assert_parent_chain_without_symlinks(path: Path) -> None:
    for component in _existing_components(path.parent):
        if not _path_exists_no_follow(component):
            continue
        mode = component.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise UnsafeTemporaryStoragePathError(
                f"Temporary storage ancestor is a symbolic link: {component}"
            )
        if not stat.S_ISDIR(mode):
            raise UnsafeTemporaryStoragePathError(
                f"Temporary storage ancestor is not a directory: {component}"
            )


def _assert_directory_without_symlinks(path: Path) -> None:
    _assert_parent_chain_without_symlinks(path)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Temporary storage directory is missing: {path}"
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise UnsafeTemporaryStoragePathError(
            f"Temporary storage root is not a real directory: {path}"
        )


def _create_directory_without_symlinks(path: Path) -> None:
    absolute = _absolute(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            current.mkdir()
            continue
        if stat.S_ISLNK(mode):
            raise UnsafeTemporaryStoragePathError(
                f"Temporary storage path crosses a symbolic link: {current}"
            )
        if not stat.S_ISDIR(mode):
            raise UnsafeTemporaryStoragePathError(
                f"Temporary storage component is not a directory: {current}"
            )


def _clear_directory_contents_no_follow(root: Path) -> None:
    _assert_parent_chain_without_symlinks(root)
    try:
        mode = root.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        root.unlink()
        return
    if not stat.S_ISDIR(mode):
        raise UnsafeTemporaryStoragePathError(
            f"Temporary storage root is not a directory: {root}"
        )
    with os.scandir(root) as entries:
        children = [Path(entry.path) for entry in entries]
    for child in children:
        _remove_entry_no_follow(child)


def _remove_entry_no_follow(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISDIR(mode):
        with os.scandir(path) as entries:
            children = [Path(entry.path) for entry in entries]
        for child in children:
            _remove_entry_no_follow(child)
        path.rmdir()
        return
    path.unlink()


__all__ = ["ManagedDatabaseCopyStore", "UnsafeTemporaryStoragePathError"]
