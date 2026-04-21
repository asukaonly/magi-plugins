#!/usr/bin/env python3
"""Generate registry.json from all plugin.toml files in plugins/.

Usage:
    python scripts/build-registry.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[import-untyped,no-redef]

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = REPO_ROOT / "plugins"
REGISTRY_PATH = REPO_ROOT / "registry.json"


def build_entry(plugin_dir: Path) -> dict | None:
    toml_path = plugin_dir / "plugin.toml"
    if not toml_path.exists():
        return None
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    meta = data.get("plugin", {})
    entry: dict = {
        "plugin_id": meta.get("id", plugin_dir.name),
        "name": meta.get("name", plugin_dir.name),
    }
    if "name_i18n" in meta:
        entry["name_i18n"] = meta["name_i18n"]
    entry["version"] = meta.get("version", "0.0.0")
    entry["path"] = f"plugins/{plugin_dir.name}"
    entry["description"] = meta.get("description", "")
    if "description_i18n" in meta:
        entry["description_i18n"] = meta["description_i18n"]
    entry["author"] = meta.get("author", "")
    entry["official"] = meta.get("official", False)
    entry["contribution_types"] = meta.get("contribution_types", [])
    entry["platforms"] = meta.get("platforms", [])
    return entry


def main() -> None:
    entries = []
    for child in sorted(PLUGINS_DIR.iterdir()):
        if not child.is_dir():
            continue
        entry = build_entry(child)
        if entry:
            entries.append(entry)
            print(f"  + {entry['plugin_id']} v{entry['version']}")

    registry = {
        "registry_version": "2",
        "repo_url": "https://github.com/asukaonly/magi-plugins.git",
        "plugins": entries,
    }

    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"\nWrote {len(entries)} plugins to {REGISTRY_PATH}")


if __name__ == "__main__":
    main()
