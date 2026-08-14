#!/usr/bin/env python3
"""Check that version-history.json only appends to a Git base revision."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[import-untyped,no-redef]

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from package_identity import (  # noqa: E402
    PackageIdentityError,
    snapshot_git_index,
    tracked_plugin_package_metadata,
    tracked_repository_file_bytes,
)
from version_history import (  # noqa: E402
    PackageVersionRecord,
    VersionHistoryError,
    assert_history_extends,
    assert_new_history_matches_current_packages,
    load_version_history,
    package_version_key,
    package_version_record,
    parse_version_history,
)

REPO_ROOT = SCRIPT_DIR.parent
VERSION_HISTORY_PATH = REPO_ROOT / "version-history.json"
REGISTRY_PATH = REPO_ROOT / "registry.json"


def _history_at_revision(revision: str) -> dict[str, PackageVersionRecord]:
    revision_check = subprocess.run(
        ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if revision_check.returncode != 0:
        raise VersionHistoryError(f"Git base revision is unavailable: {revision}")

    history_check = subprocess.run(
        ["git", "cat-file", "-e", f"{revision}:version-history.json"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if history_check.returncode != 0:
        # The first migration base legitimately has no history file.
        return {}

    result = subprocess.run(
        ["git", "show", f"{revision}:version-history.json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise VersionHistoryError(
            f"Cannot read {revision}:version-history.json: {result.stderr.strip()}"
        )
    return parse_version_history(
        result.stdout,
        label=f"{revision}:version-history.json",
    )


def _registry_at_revision(revision: str) -> dict:
    result = subprocess.run(
        ["git", "show", f"{revision}:registry.json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise VersionHistoryError(
            f"Cannot read {revision}:registry.json: {result.stderr.strip()}"
        )
    try:
        registry = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise VersionHistoryError(
            f"{revision}:registry.json is not valid JSON: {exc}"
        ) from exc
    if not isinstance(registry, dict) or not isinstance(registry.get("plugins"), list):
        raise VersionHistoryError(
            f"{revision}:registry.json must contain a plugins list"
        )
    return registry


def _commits_since(base_revision: str) -> list[str]:
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_revision, "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if ancestor.returncode != 0:
        raise VersionHistoryError(
            f"Git base revision is not an ancestor of HEAD: {base_revision}"
        )
    result = subprocess.run(
        [
            "git",
            "rev-list",
            "--reverse",
            "--topo-order",
            f"{base_revision}..HEAD",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise VersionHistoryError(
            f"Cannot inspect commits after {base_revision}: {result.stderr.strip()}"
        )
    return [revision for revision in result.stdout.splitlines() if revision]


def _package_record_at_revision(
    revision: str,
    entry: dict,
) -> tuple[str, PackageVersionRecord]:
    try:
        plugin_id = entry["plugin_id"]
        version = entry["version"]
        package_path = entry["path"]
        registry_sha256 = entry["package_sha256"]
    except KeyError as exc:
        raise VersionHistoryError(
            f"{revision}:registry.json contains an incomplete plugin entry"
        ) from exc
    if not all(
        isinstance(value, str)
        for value in (plugin_id, version, package_path, registry_sha256)
    ):
        raise VersionHistoryError(
            f"{revision}:registry.json contains invalid plugin publication fields"
        )

    key = package_version_key(plugin_id, version)
    plugin_dir = REPO_ROOT / package_path
    try:
        manifest_bytes = tracked_repository_file_bytes(
            REPO_ROOT,
            plugin_dir / "plugin.toml",
            tree_id=revision,
        )
        manifest = tomllib.loads(manifest_bytes.decode("utf-8"))
    except (PackageIdentityError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise VersionHistoryError(
            f"Cannot validate {key} manifest at commit {revision}"
        ) from exc
    metadata = manifest.get("plugin") if isinstance(manifest, dict) else None
    if not isinstance(metadata, dict) or (
        metadata.get("id") != plugin_id or metadata.get("version") != version
    ):
        raise VersionHistoryError(
            f"Registry package {key} does not match its manifest at commit {revision}"
        )

    try:
        package_metadata = tracked_plugin_package_metadata(
            REPO_ROOT,
            plugin_dir,
            tree_id=revision,
        )
    except PackageIdentityError as exc:
        raise VersionHistoryError(
            f"Cannot validate registry package {key} at commit {revision}: {exc}"
        ) from exc
    if package_metadata.package_sha256 != registry_sha256:
        raise VersionHistoryError(
            f"Registry package digest is stale for {key} at commit {revision}"
        )
    return key, package_version_record(
        package_sha256=package_metadata.package_sha256,
        executable_paths=package_metadata.executable_paths,
    )


def _package_records_across_commit_range(
    base_revision: str,
    base: dict[str, PackageVersionRecord],
    current: dict[str, PackageVersionRecord],
    current_packages: dict[str, PackageVersionRecord],
) -> dict[str, PackageVersionRecord]:
    """Find and validate every new package version published in the commit range."""

    expected = {key: current[key] for key in current.keys() - base.keys()}
    revision_histories: list[tuple[str, dict[str, PackageVersionRecord]]] = []

    for revision in _commits_since(base_revision):
        revision_history = _history_at_revision(revision)
        assert_history_extends(
            base,
            revision_history,
            base_label=f"{base_revision}:version-history.json",
        )
        revision_histories.append((revision, revision_history))
        for key, revision_record in revision_history.items():
            if key in base:
                continue
            expected_record = expected.get(key)
            if expected_record is None:
                expected[key] = revision_record
                continue
            if revision_record != expected_record:
                raise VersionHistoryError(
                    f"New package history {key} changes identity inside "
                    f"{base_revision}..HEAD at commit {revision}"
                )

    removed = sorted(expected.keys() - current.keys())
    if removed:
        detail = "\n  - ".join(removed)
        raise VersionHistoryError(
            "Package versions recorded inside the commit range cannot be removed "
            f"from final version-history.json:\n  - {detail}"
        )

    publication_records = dict(current_packages)
    found = {
        key for key, record in expected.items() if current_packages.get(key) == record
    }

    for revision, revision_history in revision_histories:
        matching_entries: dict[str, dict] = {}
        for raw_entry in _registry_at_revision(revision)["plugins"]:
            if not isinstance(raw_entry, dict):
                raise VersionHistoryError(
                    f"{revision}:registry.json contains an invalid plugin entry"
                )
            plugin_id = raw_entry.get("plugin_id")
            version = raw_entry.get("version")
            if not isinstance(plugin_id, str) or not isinstance(version, str):
                continue
            key = package_version_key(plugin_id, version)
            if key not in expected or key not in revision_history:
                continue
            if key in matching_entries:
                raise VersionHistoryError(
                    f"{revision}:registry.json contains duplicate package {key}"
                )
            matching_entries[key] = raw_entry

        for key, entry in matching_entries.items():
            verified_key, record = _package_record_at_revision(revision, entry)
            if verified_key != key or record != expected[key]:
                raise VersionHistoryError(
                    f"New package history {key} does not match the package "
                    f"published at commit {revision}"
                )
            publication_records[key] = record
            found.add(key)

    missing = sorted(expected.keys() - found)
    if missing:
        detail = "\n  - ".join(missing)
        raise VersionHistoryError(
            "New version-history entries must identify a package published in "
            f"{base_revision}..HEAD:\n  - {detail}"
        )
    return publication_records


def _current_package_records(
    registry: dict,
) -> dict[str, PackageVersionRecord]:
    """Derive current publication metadata from one frozen staged Git tree."""

    tree_id = snapshot_git_index(REPO_ROOT)
    records: dict[str, PackageVersionRecord] = {}
    for entry in registry["plugins"]:
        metadata = tracked_plugin_package_metadata(
            REPO_ROOT,
            REPO_ROOT / entry["path"],
            tree_id=tree_id,
        )
        if entry["package_sha256"] != metadata.package_sha256:
            raise VersionHistoryError(
                f"Registry package digest is stale for {entry['plugin_id']}"
            )
        key = package_version_key(entry["plugin_id"], entry["version"])
        if key in records:
            raise VersionHistoryError(f"Registry contains duplicate package {key}")
        records[key] = package_version_record(
            package_sha256=metadata.package_sha256,
            executable_paths=metadata.executable_paths,
        )
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_revision", help="Git revision to compare against")
    args = parser.parse_args()

    try:
        base = _history_at_revision(args.base_revision)
        current = load_version_history(VERSION_HISTORY_PATH)
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        current_packages = _current_package_records(registry)
        assert_history_extends(
            base,
            current,
            base_label=f"{args.base_revision}:version-history.json",
        )
        publication_records = _package_records_across_commit_range(
            args.base_revision,
            base,
            current,
            current_packages,
        )
        assert_new_history_matches_current_packages(
            base,
            current,
            publication_records,
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        print(
            f"error: registry.json cannot validate version history: {exc}",
            file=sys.stderr,
        )
        return 1
    except (PackageIdentityError, VersionHistoryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"version-history.json preserves {len(base)} published package versions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
