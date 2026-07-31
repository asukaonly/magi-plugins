#!/usr/bin/env python3
"""Git-backed publication adapter for the Magi package identity contract.

The SDK owns canonical paths, portable-path collision handling, identity
framing, and digest construction. This module only freezes and reads the Git
index, applies marketplace publication limits, rejects runtime products, maps
SDK failures, and preserves executable-path metadata beside the source digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import subprocess

from magi_plugin_sdk.package_identity import (
    PACKAGE_IDENTITY_DOMAIN,
    PACKAGE_IDENTITY_VERSION,
    SOURCE_PACKAGE_IDENTITY_PROFILE,
    CanonicalPackagePath,
    PackageFile,
    PackageIdentityContractError,
    PortablePathTracker,
    canonicalize_package_path,
    compute_package_identity_sha256,
)

PACKAGE_IDENTITY_PROFILE = SOURCE_PACKAGE_IDENTITY_PROFILE

MAX_PACKAGE_FILES = 4096
MAX_PACKAGE_FILE_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_TOTAL_BYTES = 256 * 1024 * 1024
MAX_PACKAGE_PATH_BYTES = 1024
MAX_PACKAGE_PATH_DEPTH = 32
MAX_PACKAGE_COMPONENT_BYTES = 255

GIT_OBJECT_ID_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class PackageIdentityError(ValueError):
    """Raised when a plugin cannot have one safe publication identity."""


@dataclass(frozen=True, slots=True)
class PackagePublicationMetadata:
    """Publication metadata derived from one frozen plugin package tree."""

    package_sha256: str
    executable_paths: tuple[str, ...]


def package_sha256(files: list[PackageFile]) -> str:
    """Return the SDK-defined source identity for canonical package files."""

    try:
        return compute_package_identity_sha256(
            files,
            profile=SOURCE_PACKAGE_IDENTITY_PROFILE,
        )
    except PackageIdentityContractError as exc:
        raise PackageIdentityError(
            f"Plugin package violates the Magi SDK identity contract: {exc}"
        ) from exc


def package_executable_paths(files: list[PackageFile]) -> tuple[str, ...]:
    """Return canonical executable paths without changing source identity."""

    return tuple(
        sorted(
            (
                package_file.path.relative_path
                for package_file in files
                if package_file.executable
            ),
            key=lambda path: path.encode("utf-8"),
        )
    )


def snapshot_git_index(repo_root: Path) -> str:
    """Freeze the current staged index as one immutable Git tree object."""

    tree_id = _git_bytes(repo_root.resolve(), "write-tree").decode("ascii").strip()
    if not GIT_OBJECT_ID_PATTERN.fullmatch(tree_id):
        raise PackageIdentityError("Git returned an invalid index tree object")
    return tree_id


def tracked_plugin_directories(repo_root: Path, tree_id: str) -> list[Path]:
    """List direct plugin packages containing plugin.toml in one Git tree."""

    repo_root = repo_root.resolve()
    _validate_tree_id(tree_id)
    output = _git_bytes(
        repo_root,
        "ls-tree",
        "-r",
        "-z",
        "--name-only",
        "--full-tree",
        tree_id,
        "--",
        ":(top,literal)plugins",
    )
    directories: list[Path] = []
    for raw_path in output.split(b"\0"):
        if not raw_path:
            continue
        try:
            repo_relative = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PackageIdentityError(
                "Plugin package contains a path that is not valid UTF-8"
            ) from exc
        parts = PurePosixPath(repo_relative).parts
        if len(parts) == 3 and parts[0] == "plugins" and parts[2] == "plugin.toml":
            directories.append(repo_root / Path(*parts[:2]))
    return sorted(directories)


def tracked_plugin_package_sha256(
    repo_root: Path,
    plugin_dir: Path,
    *,
    tree_id: str | None = None,
) -> str:
    """Hash every tracked regular file below one plugin directory."""

    return tracked_plugin_package_metadata(
        repo_root,
        plugin_dir,
        tree_id=tree_id,
    ).package_sha256


def tracked_plugin_package_metadata(
    repo_root: Path,
    plugin_dir: Path,
    *,
    tree_id: str | None = None,
) -> PackagePublicationMetadata:
    """Derive package digest and executable paths from one Git tree scan."""

    files = tracked_plugin_package_files(repo_root, plugin_dir, tree_id=tree_id)
    return PackagePublicationMetadata(
        package_sha256=package_sha256(files),
        executable_paths=package_executable_paths(files),
    )


def validate_plugin_worktree(repo_root: Path) -> None:
    """Require every non-ignored plugin change to be staged for generation."""

    repo_root = repo_root.resolve()
    pathspec = ":(top,literal)plugins"
    unstaged = _git_paths(
        repo_root,
        "diff",
        "--name-only",
        "-z",
        "--",
        pathspec,
    )
    if unstaged:
        listed = ", ".join(sorted(unstaged)[:5])
        suffix = "" if len(unstaged) <= 5 else ", ..."
        raise PackageIdentityError(
            "Plugin packages have unstaged changes; stage the complete package "
            f"snapshot before rebuilding the registry: {listed}{suffix}"
        )
    untracked = _git_paths(
        repo_root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        pathspec,
    )
    if untracked:
        listed = ", ".join(sorted(untracked)[:5])
        suffix = "" if len(untracked) <= 5 else ", ..."
        raise PackageIdentityError(
            "Plugin packages contain non-ignored files that are not tracked; "
            f"stage them before rebuilding the registry: {listed}{suffix}"
        )


def tracked_plugin_package_files(
    repo_root: Path,
    plugin_dir: Path,
    *,
    tree_id: str | None = None,
) -> list[PackageFile]:
    """Collect one plugin's SDK package files from one staged Git tree."""

    repo_root = repo_root.resolve()
    plugin_dir = plugin_dir.resolve()
    plugins_root = (repo_root / "plugins").resolve()
    if plugin_dir.parent != plugins_root:
        raise PackageIdentityError(
            f"Plugin package must be a direct child of {plugins_root}: {plugin_dir}"
        )

    package_relative = plugin_dir.relative_to(repo_root).as_posix()
    pathspec = f":(top,literal){package_relative}"
    if tree_id is None:
        _require_clean_package_worktree(repo_root, pathspec)
        tree_id = snapshot_git_index(repo_root)
    else:
        _validate_tree_id(tree_id)

    tree_output = _git_bytes(
        repo_root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        tree_id,
        "--",
        pathspec,
    )
    entries = _parse_git_tree_entries(tree_output)
    if not entries:
        raise PackageIdentityError(f"Plugin package has no tracked files: {plugin_dir}")
    if len(entries) > MAX_PACKAGE_FILES:
        raise PackageIdentityError(
            f"Plugin package contains more than {MAX_PACKAGE_FILES} tracked files"
        )

    path_tracker = PortablePathTracker()
    files: list[PackageFile] = []
    total_bytes = 0

    for mode, object_type, object_id, repo_relative in entries:
        if mode not in {"100644", "100755"} or object_type != "blob":
            kind = {
                "120000": "symbolic link",
                "160000": "Git submodule",
            }.get(mode, f"unsupported Git mode {mode}")
            raise PackageIdentityError(
                f"Plugin package contains a {kind}: {repo_relative}"
            )

        try:
            relative_path = PurePosixPath(repo_relative).relative_to(package_relative)
        except ValueError as exc:
            raise PackageIdentityError(
                f"Tracked path escapes plugin package: {repo_relative}"
            ) from exc

        canonical_path = _register_publication_path(
            path_tracker,
            relative_path.parts,
            source_path=relative_path.as_posix(),
        )
        _reject_runtime_product(canonical_path.relative_path)

        content = _git_bytes(repo_root, "cat-file", "blob", object_id)
        if len(content) > MAX_PACKAGE_FILE_BYTES:
            raise PackageIdentityError(
                f"Plugin package file exceeds {MAX_PACKAGE_FILE_BYTES} bytes: "
                f"{repo_relative}"
            )
        total_bytes += len(content)
        if total_bytes > MAX_PACKAGE_TOTAL_BYTES:
            raise PackageIdentityError(
                f"Plugin package exceeds {MAX_PACKAGE_TOTAL_BYTES} total bytes"
            )
        files.append(
            PackageFile(
                path=canonical_path,
                content_size=len(content),
                chunks=(content,),
                executable=mode == "100755",
            )
        )

    return files


def normalize_package_path(path: str) -> str:
    """Return the SDK canonical path used by publication metadata."""

    if not isinstance(path, str):
        raise PackageIdentityError("Plugin package path must be text")
    try:
        canonical_path = canonicalize_package_path(path.split("/"))
    except PackageIdentityContractError as exc:
        raise PackageIdentityError(
            f"Plugin package path violates the Magi SDK contract: {path!r}: {exc}"
        ) from exc
    _validate_publication_path_limits(canonical_path, source_path=path)
    return canonical_path.relative_path


def tracked_repository_file_bytes(
    repo_root: Path,
    file_path: Path,
    *,
    tree_id: str | None = None,
) -> bytes:
    """Read one regular file from a frozen staged Git tree."""

    repo_root = repo_root.resolve()
    try:
        absolute_path = (
            file_path if file_path.is_absolute() else repo_root / file_path
        ).absolute()
        relative = absolute_path.relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise PackageIdentityError(
            f"Tracked file is outside the repository: {file_path}"
        ) from exc
    if tree_id is None:
        tree_id = snapshot_git_index(repo_root)
    else:
        _validate_tree_id(tree_id)
    pathspec = f":(top,literal){relative}"
    output = _git_bytes(
        repo_root,
        "ls-tree",
        "-z",
        "--full-tree",
        tree_id,
        "--",
        pathspec,
    )
    entries = [item for item in output.split(b"\0") if item]
    if len(entries) != 1:
        raise PackageIdentityError(f"File is not uniquely tracked by Git: {relative}")
    header, separator, raw_path = entries[0].partition(b"\t")
    if not separator or raw_path.decode("utf-8") != relative:
        raise PackageIdentityError(f"Git returned an invalid entry for: {relative}")
    mode, object_type, object_id = header.decode("ascii").split()
    if mode not in {"100644", "100755"} or object_type != "blob":
        raise PackageIdentityError(f"Tracked file is not a regular file: {relative}")
    return _git_bytes(repo_root, "cat-file", "blob", object_id)


def _require_clean_package_worktree(repo_root: Path, pathspec: str) -> None:
    unstaged = _git_paths(
        repo_root,
        "diff",
        "--name-only",
        "-z",
        "--",
        pathspec,
    )
    if unstaged:
        listed = ", ".join(sorted(unstaged)[:5])
        suffix = "" if len(unstaged) <= 5 else ", ..."
        raise PackageIdentityError(
            "Plugin package has unstaged changes; stage the complete package "
            f"before rebuilding the registry: {listed}{suffix}"
        )
    untracked = _git_paths(
        repo_root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        pathspec,
    )
    if untracked:
        listed = ", ".join(sorted(untracked)[:5])
        suffix = "" if len(untracked) <= 5 else ", ..."
        raise PackageIdentityError(
            "Plugin package contains non-ignored files that are not tracked; "
            f"stage them before rebuilding the registry: {listed}{suffix}"
        )


def _parse_git_tree_entries(
    tree_output: bytes,
) -> list[tuple[str, str, str, str]]:
    entries: list[tuple[str, str, str, str]] = []
    for raw_entry in tree_output.split(b"\0"):
        if not raw_entry:
            continue
        header, separator, raw_path = raw_entry.partition(b"\t")
        if not separator:
            raise PackageIdentityError("Git returned an invalid tracked-file entry")
        header_parts = header.split()
        if len(header_parts) != 3:
            raise PackageIdentityError("Git returned an invalid tracked-file header")
        try:
            mode, object_type, object_id = (
                part.decode("ascii") for part in header_parts
            )
            repo_relative = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PackageIdentityError(
                "Plugin package contains invalid Git metadata or a non-UTF-8 path"
            ) from exc
        entries.append((mode, object_type, object_id, repo_relative))
    return entries


def _register_publication_path(
    tracker: PortablePathTracker,
    parts: tuple[str, ...],
    *,
    source_path: str,
) -> CanonicalPackagePath:
    try:
        canonical_path = tracker.add(parts)
    except PackageIdentityContractError as exc:
        raise PackageIdentityError(
            f"Plugin package path violates the Magi SDK contract: "
            f"{source_path!r}: {exc}"
        ) from exc
    _validate_publication_path_limits(canonical_path, source_path=source_path)
    return canonical_path


def _validate_publication_path_limits(
    canonical_path: CanonicalPackagePath,
    *,
    source_path: str,
) -> None:
    parts = canonical_path.relative_path.split("/")
    if len(parts) > MAX_PACKAGE_PATH_DEPTH:
        raise PackageIdentityError(f"Plugin package path is too deep: {source_path}")
    if any(
        len(component.encode("utf-8")) > MAX_PACKAGE_COMPONENT_BYTES
        for component in parts
    ):
        raise PackageIdentityError(
            f"Plugin package path component is too long: {source_path}"
        )
    if len(canonical_path.path_bytes) > MAX_PACKAGE_PATH_BYTES:
        raise PackageIdentityError(f"Plugin package path is too long: {source_path}")


def _reject_runtime_product(path: str) -> None:
    parts = path.split("/")
    name = parts[-1]
    portable_parts = [part.casefold() for part in parts]
    if portable_parts[0] == ".deps" or "__pycache__" in portable_parts:
        raise PackageIdentityError(
            f"Plugin package tracks a generated runtime directory: {path}"
        )
    if name.casefold().endswith((".pyc", ".pyo")):
        raise PackageIdentityError(
            f"Plugin package tracks a generated runtime file: {path}"
        )


def _validate_tree_id(tree_id: str) -> None:
    if not GIT_OBJECT_ID_PATTERN.fullmatch(tree_id):
        raise PackageIdentityError("Invalid Git index tree object")


def _git_paths(repo_root: Path, *args: str) -> list[str]:
    output = _git_bytes(repo_root, *args)
    paths: list[str] = []
    for raw_path in output.split(b"\0"):
        if not raw_path:
            continue
        try:
            paths.append(raw_path.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise PackageIdentityError(
                "Plugin package contains a path that is not valid UTF-8"
            ) from exc
    return paths


def _git_bytes(repo_root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PackageIdentityError(
            "Plugin package identity requires a readable Git working tree"
        ) from exc
    return result.stdout


__all__ = [
    "PACKAGE_IDENTITY_DOMAIN",
    "PACKAGE_IDENTITY_PROFILE",
    "PACKAGE_IDENTITY_VERSION",
    "PackageFile",
    "PackageIdentityError",
    "PackagePublicationMetadata",
    "normalize_package_path",
    "package_executable_paths",
    "package_sha256",
    "snapshot_git_index",
    "tracked_plugin_directories",
    "tracked_plugin_package_files",
    "tracked_plugin_package_metadata",
    "tracked_plugin_package_sha256",
    "tracked_repository_file_bytes",
    "validate_plugin_worktree",
]
