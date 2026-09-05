"""Tests for build-time marketplace manifest and registry contracts."""

from __future__ import annotations

from copy import deepcopy

from magi_plugin_sdk import (
    PluginIdentifier as SdkPluginIdentifier,
    PluginManifest as SdkPluginManifest,
    PluginRegistryIndex as SdkPluginRegistryIndex,
)
import pytest
from pydantic import ValidationError

import registry_contract
from registry_contract import (
    RegistryContractError,
    validate_package_version,
    validate_plugin_manifest,
    validate_registry_index,
)

PACKAGE_SHA256 = "a" * 64


def _manifest(**overrides):
    manifest = {
        "protocol_version": 2,
        "min_sdk_version": "0.2.0",
        "execution_mode": "trusted_process",
        "projection_sources": [],
        "settings_fields": [],
        "id": "example",
        "name": "Example",
        "version": "1.0.0",
        "kind": "plugin",
        "contribution_types": ["sensor"],
        "depends_on": [],
    }
    manifest.update(overrides)
    return manifest


def _entry(plugin_id: str = "example", **overrides):
    entry = {
        "plugin_id": plugin_id,
        "name": "Example",
        "version": "1.0.0",
        "package_sha256": PACKAGE_SHA256,
        "path": f"plugins/{plugin_id}",
        "kind": "plugin",
        "contribution_types": ["sensor"],
        "depends_on": [],
        "platforms": [],
    }
    entry.update(overrides)
    return entry


def _index(*entries):
    return {
        "registry_version": "4",
        "repo_url": "https://github.com/asukaonly/magi-plugins.git",
        "plugins": list(entries),
    }


def test_adapter_uses_authoritative_sdk_contracts() -> None:
    assert registry_contract.PluginIdentifier is SdkPluginIdentifier
    assert registry_contract.PluginManifest is SdkPluginManifest
    assert registry_contract.PluginRegistryIndex is SdkPluginRegistryIndex


def test_version_validation_uses_sdk_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed: list[str] = []

    def parse(value: str) -> tuple[int, int, int]:
        parsed.append(value)
        return 1, 2, 3

    monkeypatch.setattr(registry_contract, "parse_plugin_version", parse)

    assert validate_package_version("1.2.3", field="version") == "1.2.3"
    assert parsed == ["1.2.3"]


@pytest.mark.parametrize(
    "plugin_id",
    [
        "Bad.ID",
        "../../escape",
        "a" * 65,
        "index",
    ],
)
def test_manifest_rejects_host_invalid_plugin_ids(plugin_id: str) -> None:
    with pytest.raises(RegistryContractError) as error:
        validate_plugin_manifest(_manifest(id=plugin_id), package_name="example")
    assert isinstance(error.value.__cause__, ValidationError)


def test_manifest_rejects_invalid_kind() -> None:
    with pytest.raises(RegistryContractError, match="kind"):
        validate_plugin_manifest(
            _manifest(kind="not-a-kind"),
            package_name="example",
        )


def test_manifest_rejects_versions_longer_than_host_limit() -> None:
    with pytest.raises(RegistryContractError, match="plugin.version"):
        validate_plugin_manifest(
            _manifest(version="123456789012345678901234567890.0.0"),
            package_name="example",
        )


@pytest.mark.parametrize(
    "depends_on",
    [
        ["../../escape"],
        ["library", "library"],
        ["example"],
        [f"library-{index}" for index in range(9)],
    ],
)
def test_manifest_rejects_unsafe_direct_dependencies(
    depends_on: list[str],
) -> None:
    with pytest.raises(RegistryContractError, match="depend"):
        validate_plugin_manifest(
            _manifest(depends_on=depends_on),
            package_name="example",
        )


def test_manifest_rejects_fields_that_host_manifest_cannot_parse() -> None:
    with pytest.raises(RegistryContractError, match="entry_module"):
        validate_plugin_manifest(
            _manifest(entry_module="../plugin"),
            package_name="example",
        )

    with pytest.raises(RegistryContractError, match="contribution_types"):
        validate_plugin_manifest(
            _manifest(contribution_types=["unknown"]),
            package_name="example",
        )


@pytest.mark.parametrize("registry_version", [None, "3", "5", 4])
def test_registry_version_is_a_required_strict_protocol_gate(
    registry_version: str | int | None,
) -> None:
    registry = _index(_entry())
    if registry_version is None:
        del registry["registry_version"]
    else:
        registry["registry_version"] = registry_version

    with pytest.raises(RegistryContractError, match="registry_version"):
        validate_registry_index(registry)


def test_registry_rejects_missing_and_non_library_dependencies() -> None:
    with pytest.raises(RegistryContractError, match="missing"):
        validate_registry_index(
            _index(_entry(depends_on=["shared-library"])),
        )

    with pytest.raises(RegistryContractError, match="non-library"):
        validate_registry_index(
            _index(
                _entry(depends_on=["shared-library"]),
                _entry("shared-library"),
            )
        )


def test_registry_accepts_library_dependency_and_rejects_cycles() -> None:
    validate_registry_index(
        _index(
            _entry(depends_on=["shared-library"]),
            _entry("shared-library", kind="library", contribution_types=[]),
        )
    )

    first = _entry(
        "first-library",
        kind="library",
        contribution_types=[],
        depends_on=["second-library"],
    )
    second = _entry(
        "second-library",
        kind="library",
        contribution_types=[],
        depends_on=["first-library"],
    )
    with pytest.raises(RegistryContractError, match="cycle"):
        validate_registry_index(_index(first, second))


def test_registry_rejects_nested_contract_mismatch() -> None:
    entry = _entry()
    entry["suggestion_descriptor"] = {
        "category": "example",
        "triggers": {},
        "platform_support": [],
        "rationale": {"zh": "示例"},
    }

    with pytest.raises(RegistryContractError, match="rationale.en"):
        validate_registry_index(_index(entry))


def test_local_requirement_discriminator_matches_host_contract() -> None:
    descriptor = {
        "category": "example",
        "triggers": {},
        "platform_support": [],
        "local_requirements": [
            {
                "paths_per_platform": {
                    "linux": "/tmp/example",
                },
            }
        ],
        "rationale": {
            "zh": "示例",
            "en": "Example",
        },
    }

    with pytest.raises(RegistryContractError, match="check_kind"):
        validate_plugin_manifest(
            _manifest(suggestion_descriptor=descriptor),
            package_name="example",
        )

    with pytest.raises(RegistryContractError, match="check_kind"):
        validate_registry_index(
            _index(_entry(suggestion_descriptor=descriptor)),
        )


def test_publication_policy_rejects_unknown_capabilities() -> None:
    capability = {
        "capability": "future_capability",
        "scope": [],
    }

    with pytest.raises(RegistryContractError, match="unsupported"):
        validate_plugin_manifest(
            _manifest(
                permissions={
                    "capabilities": [capability],
                }
            ),
            package_name="example",
        )

    with pytest.raises(RegistryContractError, match="unsupported"):
        validate_registry_index(
            _index(
                _entry(capabilities=[capability]),
            )
        )


def test_publication_policy_rejects_duplicate_values() -> None:
    capability = {
        "capability": "network",
        "scope": [],
    }

    with pytest.raises(RegistryContractError, match="duplicate capability"):
        validate_plugin_manifest(
            _manifest(
                permissions={
                    "capabilities": [capability, capability],
                }
            ),
            package_name="example",
        )

    with pytest.raises(RegistryContractError, match="duplicate values"):
        validate_registry_index(
            _index(
                _entry(contribution_types=["sensor", "sensor"]),
            )
        )


def test_registry_rejects_duplicate_ids_without_mutating_input() -> None:
    registry = _index(_entry(), _entry())
    original = deepcopy(registry)

    with pytest.raises(RegistryContractError, match="duplicate"):
        validate_registry_index(registry)

    assert registry == original
