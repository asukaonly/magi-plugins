"""Tests for Git-backed plugin publication identities."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from magi_plugin_sdk.package_identity import (
    SOURCE_PACKAGE_IDENTITY_PROFILE,
    PackageFile as SdkPackageFile,
    PortablePathTracker as SdkPortablePathTracker,
    canonicalize_package_path,
    compute_package_identity_sha256 as sdk_package_sha256,
)
import pytest

import package_identity
from package_identity import (
    PackageFile,
    PackageIdentityError,
    normalize_package_path,
    package_executable_paths,
    package_sha256,
    snapshot_git_index,
    tracked_plugin_directories,
    tracked_plugin_package_files,
    tracked_plugin_package_metadata,
    tracked_plugin_package_sha256,
)

ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _git_with_input(repo: Path, *args: str, content: bytes) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=content,
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("ascii").strip()


def _new_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    package = repo / "plugins" / "demo"
    package.mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.deps/\n",
        encoding="utf-8",
    )
    (package / "plugin.toml").write_text('id = "demo"\n', encoding="utf-8")
    (package / "plugin.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(
        repo, "add", ".gitignore", "plugins/demo/plugin.toml", "plugins/demo/plugin.py"
    )
    return repo, package


def _package_file(
    path: str,
    content: bytes,
    *,
    executable: bool = False,
) -> PackageFile:
    return PackageFile(
        path=canonicalize_package_path(path.split("/")),
        content_size=len(content),
        chunks=(content,),
        executable=executable,
    )


def test_adapter_uses_host_package_identity_types() -> None:
    assert package_identity.PackageFile is SdkPackageFile
    assert package_identity.PortablePathTracker is SdkPortablePathTracker
    assert package_identity.compute_package_identity_sha256 is sdk_package_sha256


def test_package_identity_matches_host_reference_vector() -> None:
    files = [
        _package_file("plugin.toml", b'id = "demo"\n'),
        _package_file("scripts/run.py", b'print("ok")\n'),
    ]

    assert package_sha256(files) == (
        "5b341aaf7c8be8205e00a5713bc8c41ad0ce67d757142e30790e94b6defae163"
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("path", "renamed.toml"),
        ("content", b'id = "changed"\n'),
    ],
)
def test_package_identity_changes_for_every_identity_field(
    field: str,
    replacement: str | bytes,
) -> None:
    original_path = "plugin.toml"
    original_content = b'id = "demo"\n'
    changed = _package_file(
        replacement if field == "path" else original_path,
        replacement if field == "content" else original_content,
    )
    original = _package_file(original_path, original_content)

    assert package_sha256([changed]) != package_sha256([original])


def test_executable_paths_are_canonical_and_sorted_publication_metadata() -> None:
    files = [
        _package_file("z/run", b"z", executable=True),
        _package_file("ignored.py", b"ignored"),
        _package_file("a/run", b"a", executable=True),
    ]

    assert package_executable_paths(files) == ("a/run", "z/run")


def test_hashes_only_git_tracked_files_and_ignores_ignored_caches(
    tmp_path: Path,
) -> None:
    repo, package = _new_repo(tmp_path)
    expected = tracked_plugin_package_sha256(repo, package)

    cache_dir = package / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "plugin.cpython-313.pyc").write_bytes(b"runtime cache")

    assert tracked_plugin_package_sha256(repo, package) == expected


def test_rejects_non_ignored_untracked_package_files(tmp_path: Path) -> None:
    repo, package = _new_repo(tmp_path)
    (package / "new_module.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(PackageIdentityError, match="stage them"):
        tracked_plugin_package_sha256(repo, package)


def test_staged_new_file_changes_package_identity(tmp_path: Path) -> None:
    repo, package = _new_repo(tmp_path)
    before = tracked_plugin_package_sha256(repo, package)
    (package / "new_module.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", "plugins/demo/new_module.py")

    assert tracked_plugin_package_sha256(repo, package) != before


@pytest.mark.parametrize(
    "runtime_path",
    [
        ".deps/plugin.py",
        ".DEPS/plugin.py",
        "nested/__pycache__/plugin.py",
        "nested/__PYCACHE__/plugin.py",
    ],
)
def test_rejects_tracked_runtime_products(
    tmp_path: Path,
    runtime_path: str,
) -> None:
    repo, package = _new_repo(tmp_path)
    runtime_file = package / runtime_path
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", "-f", f"plugins/demo/{runtime_path}")

    with pytest.raises(PackageIdentityError, match="generated runtime directory"):
        tracked_plugin_package_sha256(repo, package)


def test_rejects_tracked_symbolic_links(tmp_path: Path) -> None:
    repo, package = _new_repo(tmp_path)
    (package / "alias.py").symlink_to("plugin.py")
    _git(repo, "add", "plugins/demo/alias.py")

    with pytest.raises(PackageIdentityError, match="symbolic link"):
        tracked_plugin_package_sha256(repo, package)


def test_rejects_portable_path_collisions(tmp_path: Path) -> None:
    repo, package = _new_repo(tmp_path)
    _git(repo, "config", "core.ignorecase", "false")
    object_id = _git_with_input(repo, "hash-object", "-w", "--stdin", content=b"x")
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"100644,{object_id},plugins/demo/Reader.py",
    )
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"100644,{object_id},plugins/demo/reader.py",
    )
    tree_id = snapshot_git_index(repo)

    with pytest.raises(PackageIdentityError, match="portable"):
        tracked_plugin_package_sha256(repo, package, tree_id=tree_id)


def test_rejects_conflicting_unicode_directory_spellings(tmp_path: Path) -> None:
    repo, package = _new_repo(tmp_path)
    _git(repo, "config", "core.precomposeunicode", "false")
    object_id = _git_with_input(repo, "hash-object", "-w", "--stdin", content=b"x")
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"100644,{object_id},plugins/demo/\u00e9/first.py",
    )
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"100644,{object_id},plugins/demo/e\u0301/second.py",
    )
    tree_id = snapshot_git_index(repo)

    with pytest.raises(PackageIdentityError, match="portable spellings"):
        tracked_plugin_package_sha256(repo, package, tree_id=tree_id)


@pytest.mark.parametrize("invalid_character", ["<", ">", '"', "|", "?", "*"])
def test_rejects_windows_invalid_path_characters(invalid_character: str) -> None:
    with pytest.raises(PackageIdentityError, match="Windows"):
        normalize_package_path(f"nested/invalid{invalid_character}name.py")


def test_keeps_publication_path_size_limits() -> None:
    with pytest.raises(PackageIdentityError, match="too deep"):
        normalize_package_path("/".join("a" for _ in range(33)))

    with pytest.raises(PackageIdentityError, match="component is too long"):
        normalize_package_path("a" * 256)


def test_executable_bit_does_not_change_source_identity(tmp_path: Path) -> None:
    repo, package = _new_repo(tmp_path)
    before = tracked_plugin_package_metadata(repo, package)
    _git(repo, "update-index", "--chmod=+x", "plugins/demo/plugin.py")
    tree_id = snapshot_git_index(repo)
    after = tracked_plugin_package_metadata(repo, package, tree_id=tree_id)

    assert after.package_sha256 == before.package_sha256
    assert before.executable_paths == ()
    assert after.executable_paths == ("plugin.py",)


def test_frozen_tree_keeps_executable_paths_with_package_bytes(tmp_path: Path) -> None:
    repo, package = _new_repo(tmp_path)
    tree_id = snapshot_git_index(repo)
    _git(repo, "update-index", "--chmod=+x", "plugins/demo/plugin.py")
    current_tree_id = snapshot_git_index(repo)

    frozen = tracked_plugin_package_metadata(repo, package, tree_id=tree_id)
    current = tracked_plugin_package_metadata(
        repo,
        package,
        tree_id=current_tree_id,
    )

    assert frozen.package_sha256 == current.package_sha256
    assert frozen.executable_paths == ()
    assert current.executable_paths == ("plugin.py",)


def test_frozen_tree_is_not_affected_by_later_index_changes(tmp_path: Path) -> None:
    repo, package = _new_repo(tmp_path)
    tree_id = snapshot_git_index(repo)
    before = tracked_plugin_package_sha256(repo, package, tree_id=tree_id)

    (package / "plugin.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", "plugins/demo/plugin.py")

    assert tracked_plugin_package_sha256(repo, package) != before
    assert tracked_plugin_package_sha256(repo, package, tree_id=tree_id) == before


def test_all_current_packages_match_host_sdk_identity() -> None:
    tree_id = snapshot_git_index(ROOT)
    plugin_dirs = tracked_plugin_directories(ROOT, tree_id)
    registry_entries = {
        entry["path"]: entry
        for entry in json.loads((ROOT / "registry.json").read_text(encoding="utf-8"))[
            "plugins"
        ]
    }

    assert len(plugin_dirs) == 24
    for plugin_dir in plugin_dirs:
        files = tracked_plugin_package_files(ROOT, plugin_dir, tree_id=tree_id)
        expected = sdk_package_sha256(
            files,
            profile=SOURCE_PACKAGE_IDENTITY_PROFILE,
        )
        package_path = plugin_dir.relative_to(ROOT).as_posix()

        assert all(isinstance(package_file, SdkPackageFile) for package_file in files)
        assert package_sha256(files) == expected
        assert registry_entries[package_path]["package_sha256"] == expected
