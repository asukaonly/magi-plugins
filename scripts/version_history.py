#!/usr/bin/env python3
"""Append-only plugin package version history."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from magi_plugin_sdk.package_identity import (
    PackageIdentityContractError,
    PortablePathTracker,
)

from package_identity import PackageIdentityError, normalize_package_path
from registry_contract import (
    RegistryContractError,
    parse_package_version,
    validate_package_version,
    validate_plugin_id,
)

VERSION_HISTORY_SCHEMA = "1"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class VersionHistoryError(ValueError):
    """Raised when immutable package-version history is invalid."""


@dataclass(frozen=True, slots=True)
class PackageVersionRecord:
    """Immutable publication metadata for one plugin package version."""

    package_sha256: str
    executable_paths: tuple[str, ...]


def load_version_history(path: Path) -> dict[str, PackageVersionRecord]:
    """Load a validated version history, or return an empty initial history."""

    if not path.exists():
        return {}
    return parse_version_history(path.read_text(encoding="utf-8"), label=str(path))


def parse_version_history(
    raw: str,
    *,
    label: str,
) -> dict[str, PackageVersionRecord]:
    """Parse and validate one version-history document."""

    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VersionHistoryError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise VersionHistoryError(f"{label} must be a JSON object")
    if payload.get("schema_version") != VERSION_HISTORY_SCHEMA:
        raise VersionHistoryError(
            f"{label} must use schema_version {VERSION_HISTORY_SCHEMA}"
        )
    packages = payload.get("packages")
    if not isinstance(packages, dict):
        raise VersionHistoryError(f"{label}.packages must be a JSON object")

    normalized: dict[str, PackageVersionRecord] = {}
    for raw_key, raw_record in packages.items():
        if not isinstance(raw_key, str) or "@" not in raw_key:
            raise VersionHistoryError(
                f"{label} contains an invalid plugin_id@version key: {raw_key!r}"
            )
        plugin_id, version = raw_key.rsplit("@", 1)
        key = package_version_key(plugin_id, version)
        if key != raw_key:
            raise VersionHistoryError(
                f"{label} contains a non-canonical package version key: {raw_key!r}"
            )
        if not isinstance(raw_record, dict) or set(raw_record) != {
            "package_sha256",
            "executable_paths",
        }:
            raise VersionHistoryError(
                f"{label} must record package_sha256 and executable_paths "
                f"for {raw_key!r}"
            )
        executable_paths = raw_record["executable_paths"]
        if not isinstance(executable_paths, list):
            raise VersionHistoryError(
                f"{label} contains invalid executable_paths for {raw_key!r}"
            )
        try:
            normalized[raw_key] = package_version_record(
                package_sha256=raw_record["package_sha256"],
                executable_paths=executable_paths,
            )
        except VersionHistoryError as exc:
            raise VersionHistoryError(
                f"{label} contains invalid {raw_key!r}: {exc}"
            ) from exc
    return normalized


def package_version_record(
    *,
    package_sha256: str,
    executable_paths: list[str] | tuple[str, ...],
) -> PackageVersionRecord:
    """Validate and normalize one immutable package publication record."""

    if not isinstance(package_sha256, str) or not SHA256_PATTERN.fullmatch(
        package_sha256
    ):
        raise VersionHistoryError("Invalid package SHA-256")

    canonical_paths: list[str] = []
    for raw_path in executable_paths:
        if not isinstance(raw_path, str):
            raise VersionHistoryError("Executable paths must be strings")
        try:
            canonical_path = normalize_package_path(raw_path)
        except PackageIdentityError as exc:
            raise VersionHistoryError(f"Invalid executable path: {raw_path!r}") from exc
        if canonical_path != raw_path:
            raise VersionHistoryError(f"Executable path is not canonical: {raw_path!r}")
        canonical_paths.append(canonical_path)

    ordered_paths = sorted(canonical_paths, key=lambda path: path.encode("utf-8"))
    if canonical_paths != ordered_paths:
        raise VersionHistoryError("Executable paths must be sorted")
    if len(canonical_paths) != len(set(canonical_paths)):
        raise VersionHistoryError("Executable paths must not contain duplicates")

    portable_paths = PortablePathTracker()
    try:
        for path in canonical_paths:
            portable_paths.add(path.split("/"))
    except PackageIdentityContractError as exc:
        raise VersionHistoryError(
            "Executable paths conflict under portable path rules"
        ) from exc

    return PackageVersionRecord(
        package_sha256=package_sha256,
        executable_paths=tuple(canonical_paths),
    )


def package_version_key(plugin_id: str, version: str) -> str:
    """Return one unambiguous plugin_id@MAJOR.MINOR.PATCH history key."""

    try:
        validate_plugin_id(plugin_id, field="package history plugin id")
    except RegistryContractError as exc:
        raise VersionHistoryError(
            f"Invalid plugin id for package history: {plugin_id!r}"
        ) from exc
    try:
        validate_package_version(version, field="package history version")
    except RegistryContractError as exc:
        raise VersionHistoryError(
            "Plugin version must use canonical MAJOR.MINOR.PATCH form: " f"{version!r}"
        ) from exc
    return f"{plugin_id}@{version}"


def assert_current_version_is_latest(
    history: dict[str, PackageVersionRecord],
    *,
    plugin_id: str,
    version: str,
) -> None:
    """Reject registry downgrade or lower-version insertion for one package."""

    package_version_key(plugin_id, version)
    current_parts = parse_package_version(
        version,
        field=f"package history version for {plugin_id}",
    )
    historical_versions: list[tuple[tuple[int, int, int], str]] = []
    prefix = f"{plugin_id}@"
    for key in history:
        if not key.startswith(prefix):
            continue
        historical_version = key[len(prefix) :]
        package_version_key(plugin_id, historical_version)
        historical_versions.append(
            (
                parse_package_version(
                    historical_version,
                    field=f"package history version for {plugin_id}",
                ),
                historical_version,
            )
        )
    if not historical_versions:
        return
    latest_parts, latest_version = max(historical_versions)
    if current_parts < latest_parts:
        raise VersionHistoryError(
            f"Registry package {plugin_id}@{version} is older than published "
            f"{plugin_id}@{latest_version}; current registry versions cannot "
            "move backwards."
        )


def bind_package_version(
    history: dict[str, PackageVersionRecord],
    *,
    plugin_id: str,
    version: str,
    package_sha256: str,
    executable_paths: list[str] | tuple[str, ...],
) -> None:
    """Add immutable version metadata or reject an attempted rewrite."""

    key = package_version_key(plugin_id, version)
    current = package_version_record(
        package_sha256=package_sha256,
        executable_paths=executable_paths,
    )
    previous = history.get(key)
    if previous is not None and previous != current:
        raise VersionHistoryError(
            f"Published package version {key} is immutable: "
            "its package contents or executable paths changed. "
            "Bump the plugin version before changing published metadata."
        )
    history[key] = current


def assert_history_extends(
    base: dict[str, PackageVersionRecord],
    current: dict[str, PackageVersionRecord],
    *,
    base_label: str,
) -> None:
    """Reject deletion or mutation of any historical package-version key."""

    errors: list[str] = []
    for key, expected_record in sorted(base.items()):
        actual_record = current.get(key)
        if actual_record is None:
            errors.append(f"deleted {key}")
        elif actual_record != expected_record:
            errors.append(f"changed {key}")
    if errors:
        detail = "\n  - ".join(errors)
        raise VersionHistoryError(
            f"version-history.json must append to {base_label}; it cannot rewrite "
            f"published versions:\n  - {detail}"
        )


def assert_new_history_matches_current_packages(
    base: dict[str, PackageVersionRecord],
    current: dict[str, PackageVersionRecord],
    current_packages: dict[str, PackageVersionRecord],
) -> None:
    """Require every newly reserved version to be the current package."""

    errors: list[str] = []
    for key in sorted(current.keys() - base.keys()):
        expected_record = current_packages.get(key)
        if expected_record is None:
            errors.append(f"{key} does not identify a current registry package")
        elif current[key] != expected_record:
            errors.append(f"{key} does not match the current package metadata")
    if errors:
        detail = "\n  - ".join(errors)
        raise VersionHistoryError(
            "New version-history entries must be generated from current registry "
            f"packages:\n  - {detail}"
        )


def write_version_history(
    path: Path,
    history: dict[str, PackageVersionRecord],
) -> None:
    """Write one canonical, key-sorted version history."""

    payload = {
        "schema_version": VERSION_HISTORY_SCHEMA,
        "packages": {
            key: {
                "package_sha256": record.package_sha256,
                "executable_paths": list(record.executable_paths),
            }
            for key, record in sorted(history.items())
        },
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "VERSION_HISTORY_SCHEMA",
    "PackageVersionRecord",
    "VersionHistoryError",
    "assert_current_version_is_latest",
    "assert_history_extends",
    "assert_new_history_matches_current_packages",
    "bind_package_version",
    "load_version_history",
    "package_version_key",
    "package_version_record",
    "parse_version_history",
    "write_version_history",
]
