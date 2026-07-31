#!/usr/bin/env python3
"""Thin publication-policy adapter over the authoritative Magi plugin SDK.

The SDK owns manifest fields, registry fields, identifiers, versions, and
dependency-graph rules. This module only translates SDK validation failures
into the generator's stable error type and enforces repository publication
policies that are intentionally stricter than the forward-compatible host.
"""

from __future__ import annotations

from typing import Any, get_args

from magi_plugin_sdk import (
    ContributionType,
    PluginIdentifier,
    PluginManifest,
    PluginRegistryIndex,
    parse_plugin_version,
)
from pydantic import TypeAdapter, ValidationError

KNOWN_CAPABILITIES = frozenset(
    {
        "screen_recording",
        "accessibility",
        "calendar",
        "photos",
        "contacts",
        "system_media",
        "filesystem_read",
        "filesystem_write",
        "network",
        "subprocess",
    }
)
_KNOWN_CONTRIBUTION_TYPES = frozenset(item.value for item in ContributionType)
_PLUGIN_IDENTIFIER_ADAPTER = TypeAdapter(PluginIdentifier)


class RegistryContractError(ValueError):
    """Raised when generated marketplace data violates the host contract."""


def _sdk_registry_version() -> str:
    """Read the one supported registry protocol directly from the SDK model."""

    try:
        annotation = PluginRegistryIndex.model_fields["registry_version"].annotation
    except (AttributeError, KeyError) as exc:
        raise RegistryContractError(
            "Installed magi-plugin-sdk does not expose the registry version contract"
        ) from exc
    versions = get_args(annotation)
    if len(versions) != 1 or not isinstance(versions[0], str):
        raise RegistryContractError(
            "Installed magi-plugin-sdk does not expose one registry protocol version"
        )
    return versions[0]


REGISTRY_VERSION = _sdk_registry_version()


def validate_plugin_id(value: Any, *, field: str) -> str:
    """Validate one identifier with the SDK's public identifier contract."""

    try:
        return _PLUGIN_IDENTIFIER_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise _sdk_contract_error(field, exc) from exc


def validate_package_version(value: Any, *, field: str) -> str:
    """Validate one version with the SDK's canonical version parser."""

    parse_package_version(value, field=field)
    return value


def parse_package_version(value: Any, *, field: str) -> tuple[int, int, int]:
    """Parse one version with the SDK's canonical version parser."""

    try:
        return parse_plugin_version(value)
    except (TypeError, ValueError) as exc:
        raise RegistryContractError(
            f"{field} violates the Magi SDK version contract: {exc}"
        ) from exc


def validate_plugin_manifest(meta: Any, *, package_name: str) -> None:
    """Validate one manifest with the SDK plus repository publication policy."""

    context = f"{package_name}.plugin"
    try:
        manifest = PluginManifest.model_validate(meta)
    except ValidationError as exc:
        raise _sdk_contract_error(context, exc) from exc

    _validate_contribution_policy(
        manifest.contribution_types,
        field=f"{context}.contribution_types",
    )
    _validate_capability_policy(
        manifest.capabilities,
        field=f"{context}.permissions.capabilities",
    )


def validate_registry_entries(entries: list[dict[str, Any]]) -> None:
    """Validate registry entries through the SDK's complete index contract."""

    validate_registry_index(
        {
            "registry_version": REGISTRY_VERSION,
            "repo_url": "",
            "plugins": entries,
        }
    )


def validate_registry_index(value: Any) -> None:
    """Validate one generated registry with the SDK and publication policy."""

    try:
        registry = PluginRegistryIndex.model_validate(value)
    except ValidationError as exc:
        raise _sdk_contract_error("registry", exc) from exc

    for index, entry in enumerate(registry.plugins):
        context = f"registry.plugins[{index}]"
        _validate_contribution_policy(
            entry.contribution_types,
            field=f"{context}.contribution_types",
        )
        _validate_capability_policy(
            entry.capabilities,
            field=f"{context}.capabilities",
        )


def _validate_contribution_policy(values: list[Any], *, field: str) -> None:
    """Only publish contribution types supported by this marketplace."""

    names = [
        value.value if isinstance(value, ContributionType) else value
        for value in values
    ]
    unknown = sorted(set(names) - _KNOWN_CONTRIBUTION_TYPES)
    if unknown:
        raise RegistryContractError(
            f"{field} contains unsupported values: {', '.join(unknown)}"
        )
    if len(names) != len(set(names)):
        raise RegistryContractError(f"{field} cannot contain duplicate values")


def _validate_capability_policy(capabilities: list[Any], *, field: str) -> None:
    """Only publish known, uniquely declared user-consent capabilities."""

    seen: set[str] = set()
    for index, capability in enumerate(capabilities):
        name = capability.capability
        if name not in KNOWN_CAPABILITIES:
            raise RegistryContractError(
                f"{field}[{index}].capability is unsupported: {name}"
            )
        if name in seen:
            raise RegistryContractError(f"{field} contains duplicate capability {name}")
        seen.add(name)


def _sdk_contract_error(context: str, exc: ValidationError) -> RegistryContractError:
    """Return one stable wrapper without copying SDK field-validation logic."""

    details: list[str] = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        message = str(error["msg"])
        qualified_location = f"{context}.{location}" if location else context
        details.append(f"{qualified_location}: {message}")
    summary = "; ".join(details) or "unknown validation failure"
    return RegistryContractError(f"{context} violates the Magi SDK contract: {summary}")


__all__ = [
    "KNOWN_CAPABILITIES",
    "REGISTRY_VERSION",
    "RegistryContractError",
    "parse_package_version",
    "validate_package_version",
    "validate_plugin_id",
    "validate_plugin_manifest",
    "validate_registry_entries",
    "validate_registry_index",
]
