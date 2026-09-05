"""Crash-recoverable storage for disposable NetEase database copies."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from magi_plugin_sdk.fs import path_is_link


class UnsafeTemporaryStoragePathError(RuntimeError):
    """Raised when temporary storage crosses a symbolic link."""


class NeteaseTemporaryDatabaseStore:
    """Own the private directory used for disposable database copies."""

    def __init__(self, *, temp_root: Path | None = None) -> None:
        self._default_root = _absolute(temp_root) if temp_root is not None else None
        self._prepared_root: Path | None = None

    @property
    def root(self) -> Path:
        root = self._prepared_root or self._default_root
        if root is None:
            raise ValueError("Temporary storage requires a host-bound connection directory")
        return root

    def prepare(self, temp_root: Path | None = None) -> Path:
        """Sweep leftovers from an earlier process and create a real directory."""

        root = _absolute(temp_root) if temp_root is not None else self.root
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
        candidate = _absolute(copy_dir)
        if candidate.parent != self.root:
            raise UnsafeTemporaryStoragePathError(
                f"Temporary copy is outside its managed directory: {candidate}"
            )
        _remove_entry_no_follow(candidate)

    def clear(self, temp_root: Path | None = None) -> None:
        root = _absolute(temp_root) if temp_root is not None else self.root
        _clear_directory_contents_no_follow(root)
        if self._prepared_root == root:
            self._prepared_root = None


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


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
        try:
            component_stat = component.lstat()
        except FileNotFoundError:
            continue
        if path_is_link(component, path_stat=component_stat):
            raise UnsafeTemporaryStoragePathError(
                f"Temporary storage ancestor is a link or reparse point: {component}"
            )
        if not stat.S_ISDIR(component_stat.st_mode):
            raise UnsafeTemporaryStoragePathError(
                f"Temporary storage ancestor is not a directory: {component}"
            )


def _assert_directory_without_symlinks(path: Path) -> None:
    _assert_parent_chain_without_symlinks(path)
    try:
        path_stat = path.lstat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Temporary storage directory is missing: {path}"
        ) from exc
    if path_is_link(path, path_stat=path_stat) or not stat.S_ISDIR(path_stat.st_mode):
        raise UnsafeTemporaryStoragePathError(
            f"Temporary storage root is not a real directory: {path}"
        )


def _create_directory_without_symlinks(path: Path) -> None:
    absolute = _absolute(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            current.mkdir()
            continue
        if path_is_link(current, path_stat=current_stat):
            raise UnsafeTemporaryStoragePathError(
                f"Temporary storage path crosses a link or reparse point: {current}"
            )
        if not stat.S_ISDIR(current_stat.st_mode):
            raise UnsafeTemporaryStoragePathError(
                f"Temporary storage component is not a directory: {current}"
            )


def _clear_directory_contents_no_follow(root: Path) -> None:
    _assert_parent_chain_without_symlinks(root)
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return
    if path_is_link(root, path_stat=root_stat):
        _remove_link_or_reparse(root, root_stat)
        return
    if not stat.S_ISDIR(root_stat.st_mode):
        raise UnsafeTemporaryStoragePathError(
            f"Temporary storage root is not a directory: {root}"
        )
    with os.scandir(root) as entries:
        children = [Path(entry.path) for entry in entries]
    for child in children:
        _remove_entry_no_follow(child)


def _remove_entry_no_follow(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return
    if path_is_link(path, path_stat=path_stat):
        _remove_link_or_reparse(path, path_stat)
        return
    if stat.S_ISDIR(path_stat.st_mode):
        with os.scandir(path) as entries:
            children = [Path(entry.path) for entry in entries]
        for child in children:
            _remove_entry_no_follow(child)
        path.rmdir()
        return
    path.unlink()


def _remove_link_or_reparse(path: Any, path_stat: Any) -> None:
    """Remove one link-like directory entry without traversing its target."""

    if stat.S_ISDIR(path_stat.st_mode):
        path.rmdir()
    else:
        path.unlink()


__all__ = ["NeteaseTemporaryDatabaseStore", "UnsafeTemporaryStoragePathError"]
