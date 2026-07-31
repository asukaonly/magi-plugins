"""Tests for immutable plugin package version history."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from version_history import (
    PackageVersionRecord,
    VersionHistoryError,
    assert_current_version_is_latest,
    assert_history_extends,
    assert_new_history_matches_current_packages,
    bind_package_version,
    load_version_history,
    package_version_key,
    package_version_record,
    parse_version_history,
    write_version_history,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
CHECK_SCRIPT = Path(__file__).resolve().parent / "check-version-history.py"


def _record(
    package_sha256: str,
    executable_paths: tuple[str, ...] = (),
) -> PackageVersionRecord:
    return package_version_record(
        package_sha256=package_sha256,
        executable_paths=executable_paths,
    )


def _load_history_check_module():
    spec = importlib.util.spec_from_file_location(
        "check_version_history",
        CHECK_SCRIPT,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_bind_package_version_is_append_only() -> None:
    history = {"demo@1.0.0": _record(SHA_A)}

    bind_package_version(
        history,
        plugin_id="demo",
        version="1.0.0",
        package_sha256=SHA_A,
        executable_paths=(),
    )

    with pytest.raises(VersionHistoryError, match="Bump the plugin version"):
        bind_package_version(
            history,
            plugin_id="demo",
            version="1.0.0",
            package_sha256=SHA_B,
            executable_paths=(),
        )


def test_same_version_cannot_change_executable_paths() -> None:
    history = {"demo@1.0.0": _record(SHA_A, ("bin/helper",))}

    with pytest.raises(VersionHistoryError, match="Bump the plugin version"):
        bind_package_version(
            history,
            plugin_id="demo",
            version="1.0.0",
            package_sha256=SHA_A,
            executable_paths=("bin/other-helper",),
        )


def test_new_version_appends_without_rewriting_old_version() -> None:
    history = {"demo@1.0.0": _record(SHA_A)}

    bind_package_version(
        history,
        plugin_id="demo",
        version="1.0.1",
        package_sha256=SHA_B,
        executable_paths=("bin/helper",),
    )

    assert history == {
        "demo@1.0.0": _record(SHA_A),
        "demo@1.0.1": _record(SHA_B, ("bin/helper",)),
    }


def test_current_version_cannot_insert_a_lower_version() -> None:
    history = {"demo@2.0.0": _record(SHA_A)}

    with pytest.raises(VersionHistoryError, match="cannot move backwards"):
        assert_current_version_is_latest(
            history,
            plugin_id="demo",
            version="1.5.0",
        )


def test_current_version_cannot_point_back_to_an_old_identity() -> None:
    history = {
        "demo@1.0.0": _record(SHA_A),
        "demo@2.0.0": _record(SHA_B),
    }

    with pytest.raises(VersionHistoryError, match="cannot move backwards"):
        assert_current_version_is_latest(
            history,
            plugin_id="demo",
            version="1.0.0",
        )


@pytest.mark.parametrize(
    "version",
    [
        "1.0",
        "01.0.0",
        "1.0.0-alpha",
        "1.0.0+build",
        "v1.0.0",
        "123456789012345678901234567890.0.0",
    ],
)
def test_package_versions_require_canonical_three_part_form(version: str) -> None:
    with pytest.raises(VersionHistoryError, match="MAJOR.MINOR.PATCH"):
        package_version_key("demo", version)


@pytest.mark.parametrize("plugin_id", ["", "@demo", "demo@next", "demo/child"])
def test_history_keys_require_unambiguous_plugin_ids(plugin_id: str) -> None:
    with pytest.raises(VersionHistoryError, match="Invalid plugin id"):
        package_version_key(plugin_id, "1.0.0")


@pytest.mark.parametrize(
    "current",
    [
        {},
        {"demo@1.0.0": _record(SHA_B)},
        {"demo@1.0.0": _record(SHA_A, ("helper",))},
    ],
)
def test_history_must_preserve_every_base_entry(
    current: dict[str, PackageVersionRecord],
) -> None:
    with pytest.raises(VersionHistoryError, match="cannot rewrite"):
        assert_history_extends(
            {"demo@1.0.0": _record(SHA_A)},
            current,
            base_label="base",
        )


def test_history_may_only_append(tmp_path: Path) -> None:
    path = tmp_path / "version-history.json"
    history = {
        "zeta@1.0.0": _record(SHA_B, ("z-helper",)),
        "alpha@1.0.0": _record(SHA_A),
    }

    write_version_history(path, history)
    loaded = load_version_history(path)

    assert loaded == history
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert list(payload["packages"]) == ["alpha@1.0.0", "zeta@1.0.0"]
    assert payload["packages"]["zeta@1.0.0"] == {
        "package_sha256": SHA_B,
        "executable_paths": ["z-helper"],
    }


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (SHA_A, "package_sha256 and executable_paths"),
        (
            {
                "package_sha256": SHA_A,
                "executable_paths": ["z-helper", "a-helper"],
            },
            "must be sorted",
        ),
        (
            {
                "package_sha256": SHA_A,
                "executable_paths": ["helper", "helper"],
            },
            "must not contain duplicates",
        ),
        (
            {
                "package_sha256": SHA_A,
                "executable_paths": ["nested/../helper"],
            },
            "Invalid executable path",
        ),
    ],
)
def test_history_requires_final_publication_metadata_schema(
    record: object,
    message: str,
) -> None:
    raw = json.dumps(
        {
            "schema_version": "1",
            "packages": {"demo@1.0.0": record},
        }
    )

    with pytest.raises(VersionHistoryError, match=message):
        parse_version_history(raw, label="history")


@pytest.mark.parametrize(
    "executable_paths",
    [
        ["Helper", "helper"],
        ["É/helper", "é/helper"],
    ],
)
def test_history_rejects_portable_executable_path_collisions(
    executable_paths: list[str],
) -> None:
    raw = json.dumps(
        {
            "schema_version": "1",
            "packages": {
                "demo@1.0.0": {
                    "package_sha256": SHA_A,
                    "executable_paths": executable_paths,
                }
            },
        }
    )

    with pytest.raises(VersionHistoryError, match="portable path rules"):
        parse_version_history(raw, label="history")


def test_new_history_entries_must_match_a_current_registry_package() -> None:
    base = {"demo@1.0.0": _record(SHA_A)}
    current = {
        **base,
        "demo@1.0.1": _record(SHA_B, ("bin/helper",)),
    }

    assert_new_history_matches_current_packages(
        base,
        current,
        {"demo@1.0.1": _record(SHA_B, ("bin/helper",))},
    )

    with pytest.raises(VersionHistoryError, match="current registry package"):
        assert_new_history_matches_current_packages(base, current, {})

    with pytest.raises(VersionHistoryError, match="current package metadata"):
        assert_new_history_matches_current_packages(
            base,
            current,
            {"demo@1.0.1": _record(SHA_A, ("bin/helper",))},
        )

    with pytest.raises(VersionHistoryError, match="current package metadata"):
        assert_new_history_matches_current_packages(
            base,
            current,
            {"demo@1.0.1": _record(SHA_B, ("bin/other-helper",))},
        )


def test_history_check_derives_permissions_from_current_frozen_tree(
    tmp_path: Path,
) -> None:
    checker = _load_history_check_module()
    repo = tmp_path / "repo"
    package = repo / "plugins" / "demo"
    package.mkdir(parents=True)
    _git(repo, "init", "-q")
    (package / "plugin.toml").write_text(
        '[plugin]\nid = "demo"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    (package / "helper").write_text("#!/bin/sh\n", encoding="utf-8")
    _git(repo, "add", "plugins/demo/plugin.toml", "plugins/demo/helper")
    _git(repo, "update-index", "--chmod=+x", "plugins/demo/helper")

    checker.REPO_ROOT = repo
    tree_id = checker.snapshot_git_index(repo)
    metadata = checker.tracked_plugin_package_metadata(
        repo,
        package,
        tree_id=tree_id,
    )
    registry = {
        "plugins": [
            {
                "plugin_id": "demo",
                "version": "1.0.0",
                "path": "plugins/demo",
                "package_sha256": metadata.package_sha256,
            }
        ]
    }

    records = checker._current_package_records(registry)

    assert records["demo@1.0.0"] == _record(
        metadata.package_sha256,
        ("helper",),
    )
