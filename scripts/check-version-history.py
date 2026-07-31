#!/usr/bin/env python3
"""Check that version-history.json only appends to a Git base revision."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from package_identity import (  # noqa: E402
    PackageIdentityError,
    snapshot_git_index,
    tracked_plugin_package_metadata,
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
        assert_new_history_matches_current_packages(
            base,
            current,
            current_packages,
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
