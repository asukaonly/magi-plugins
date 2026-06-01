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
OFFICIAL_ALLOWLIST_PATH = REPO_ROOT / "official-plugins.json"

# Authoritative known-capability enum. The wire model (magi SDK) is permissive
# (str) for forward-compat; THIS is the gate that keeps typos / unknown
# capabilities out of registry.json. Adding a capability is a deliberate act:
# update this set AND the magi SDK + frontend category map together.
KNOWN_CAPABILITIES = {
    "screen_recording", "accessibility", "calendar", "photos",
    "contacts", "system_media",
    "filesystem_read", "filesystem_write", "network", "subprocess",
}


def load_official_ids() -> set[str]:
    """Maintainer-controlled set of plugin_ids allowed to be `official`.

    Authority for the `official` flag lives here, NOT in each plugin's
    plugin.toml — a third-party PR touching only plugins/<their-plugin>/
    cannot grant itself official status.
    """
    if not OFFICIAL_ALLOWLIST_PATH.exists():
        print(
            f"note: {OFFICIAL_ALLOWLIST_PATH.name} not found; all plugins "
            f"marked non-official",
            file=sys.stderr,
        )
        return set()
    try:
        with open(OFFICIAL_ALLOWLIST_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        sys.exit(f"error: {OFFICIAL_ALLOWLIST_PATH.name} is not valid JSON: {exc}")
    return set(data.get("official_plugin_ids", []))


def build_entry(plugin_dir: Path, official_ids: set[str]) -> dict | None:
    toml_path = plugin_dir / "plugin.toml"
    if not toml_path.exists():
        return None
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    meta = data.get("plugin", {})
    plugin_id = meta.get("id", plugin_dir.name)
    entry: dict = {
        "plugin_id": plugin_id,
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
    self_declared = bool(meta.get("official", False))
    entry["official"] = plugin_id in official_ids
    if self_declared and not entry["official"]:
        print(
            f"  ! {plugin_id}: plugin.toml self-declares official=true but is "
            f"not in official-plugins.json — ignored (allowlist is authoritative)",
            file=sys.stderr,
        )
    # kind: "plugin" (default) or "library". Libraries are hidden from
    # market listings and only installed as dep closure of a plugin.
    kind = meta.get("kind", "plugin")
    if kind != "plugin":
        entry["kind"] = kind
    entry["contribution_types"] = meta.get("contribution_types", [])
    permissions = meta.get("permissions", {}) or {}
    capabilities = permissions.get("capabilities", [])
    if capabilities:
        entry["capabilities"] = capabilities  # verbatim; validated in main()
    # depends_on: list of plugin_ids this plugin imports from (typically
    # library packages). The host resolves the closure on install.
    depends_on = meta.get("depends_on", [])
    if depends_on:
        entry["depends_on"] = depends_on
    entry["platforms"] = meta.get("platforms", [])
    return entry


def main() -> None:
    official_ids = load_official_ids()
    entries = []
    for child in sorted(PLUGINS_DIR.iterdir()):
        if not child.is_dir():
            continue
        entry = build_entry(child, official_ids)
        if entry:
            entries.append(entry)
            print(f"  + {entry['plugin_id']} v{entry['version']}")

    unknown: list[str] = []
    for entry in entries:
        for cap in entry.get("capabilities", []):
            name = cap.get("capability") if isinstance(cap, dict) else None
            if name not in KNOWN_CAPABILITIES:
                unknown.append(f"{entry['plugin_id']}: {name!r}")
    if unknown:
        print("\nERROR: unknown capability(ies) declared:", file=sys.stderr)
        for u in unknown:
            print(f"  ! {u}", file=sys.stderr)
        print(
            "Allowed: " + ", ".join(sorted(KNOWN_CAPABILITIES)),
            file=sys.stderr,
        )
        sys.exit(1)

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
