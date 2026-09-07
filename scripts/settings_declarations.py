"""Static authoring access to the SDK-validated manifest settings catalog."""
from __future__ import annotations

import tomllib
from pathlib import Path

from magi_plugin_sdk import PluginManifest

CATALOG_KEYS = (
    "settings_fields", "activation_flow", "settings_actions",
    "settings_resources", "settings_ui_blocks",
)


def read_manifest(path: Path) -> PluginManifest:
    """Read only the requested manifest, independent of runtime availability."""
    metadata = tomllib.loads(path.read_text(encoding="utf-8"))["plugin"]
    return PluginManifest.model_validate(metadata)


def settings_catalog(manifest: PluginManifest) -> dict[str, object]:
    """Serialize every reviewed declaration, including hidden and setup fields."""
    return manifest.model_dump(mode="json", include=set(CATALOG_KEYS))
