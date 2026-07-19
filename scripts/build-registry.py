#!/usr/bin/env python3
"""Generate registry.json from all plugin.toml files in plugins/.

Usage:
    python scripts/build-registry.py
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from pathlib import PurePosixPath
from xml.etree import ElementTree

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
ASSET_ICON_PREFIX = "asset:"
MAX_ICON_BYTES = 64 * 1024
ICON_MIME_TYPES = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}
FORBIDDEN_SVG_ELEMENTS = {
    "audio",
    "embed",
    "foreignobject",
    "iframe",
    "image",
    "object",
    "script",
    "style",
    "use",
    "video",
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


def _asset_icon_path(plugin_dir: Path, icon: str) -> Path | None:
    if not icon.startswith(ASSET_ICON_PREFIX):
        return None
    raw_path = icon.removeprefix(ASSET_ICON_PREFIX).strip()
    relative = PurePosixPath(raw_path)
    if (
        not raw_path
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"Invalid plugin icon asset path: {icon}")
    root = plugin_dir.resolve()
    candidate = plugin_dir / Path(*relative.parts)
    if candidate.is_symlink():
        raise ValueError(f"Plugin icon asset cannot be a symlink: {icon}")
    path = candidate.resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Plugin icon asset escapes package directory: {icon}")
    return path


def _validate_svg_icon(data: bytes, *, path: Path) -> None:
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError(f"Plugin icon SVG cannot declare entities: {path}")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Plugin icon SVG is invalid: {path}") from exc
    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        raise ValueError(f"Plugin icon SVG must have an <svg> root: {path}")
    for element in root.iter():
        element_name = element.tag.rsplit("}", 1)[-1].lower()
        if element_name in FORBIDDEN_SVG_ELEMENTS:
            raise ValueError(
                f"Plugin icon SVG contains forbidden <{element_name}> element: {path}"
            )
        for raw_name, raw_value in element.attrib.items():
            name = raw_name.rsplit("}", 1)[-1].lower()
            value = str(raw_value).strip().lower()
            if (
                name.startswith("on")
                or name in {"href", "src", "style"}
                or "url(" in value
                or value.startswith(("data:", "http:", "https:", "//"))
            ):
                raise ValueError(
                    f"Plugin icon SVG contains forbidden attribute {name}: {path}"
                )


def encode_icon_asset(plugin_dir: Path, icon: str) -> str | None:
    path = _asset_icon_path(plugin_dir, icon)
    if path is None:
        return None
    if not path.is_file():
        raise ValueError(f"Plugin icon asset does not exist: {path}")
    suffix = path.suffix.lower()
    mime_type = ICON_MIME_TYPES.get(suffix)
    if mime_type is None:
        raise ValueError(f"Unsupported plugin icon format: {path}")
    data = path.read_bytes()
    if not data or len(data) > MAX_ICON_BYTES:
        raise ValueError(
            f"Plugin icon must be between 1 and {MAX_ICON_BYTES} bytes: {path}"
        )
    if suffix == ".svg":
        _validate_svg_icon(data, path=path)
    elif suffix == ".png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"Plugin icon is not a valid PNG: {path}")
    elif suffix == ".webp" and not (
        data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    ):
        raise ValueError(f"Plugin icon is not a valid WebP image: {path}")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def build_entry(plugin_dir: Path, official_ids: set[str]) -> dict | None:
    toml_path = plugin_dir / "plugin.toml"
    if not toml_path.exists():
        return None
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    meta = data.get("plugin", {})
    plugin_id = meta.get("id", plugin_dir.name)
    default_settings = meta.get("default_settings") or {}
    default_sensors = (
        (default_settings.get("sensors") or {})
        if isinstance(default_settings, dict)
        else {}
    )
    if (
        meta.get("kind", "plugin") != "library"
        and "sensor" in meta.get("contribution_types", [])
        and len(default_sensors) > 1
    ):
        raise ValueError(
            f"{plugin_id} bundles multiple independently installable sources: "
            f"{', '.join(sorted(default_sensors))}"
        )
    entry: dict = {
        "plugin_id": plugin_id,
        "name": meta.get("name", plugin_dir.name),
    }
    if "name_i18n" in meta:
        entry["name_i18n"] = meta["name_i18n"]
    icon = str(meta.get("icon", "") or "").strip()
    if icon:
        entry["icon"] = icon
        icon_data = encode_icon_asset(plugin_dir, icon)
        if icon_data is not None:
            entry["icon_data"] = icon_data
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
    display_group = meta.get("display_group", {})
    if display_group:
        entry["display_group"] = display_group
    entry["platforms"] = meta.get("platforms", [])
    # Privacy-transparency signal -> "Local only" marketplace badge. Prefer the
    # top-level declaration, falling back to the suggestion_descriptor's
    # data_locality (where most plugins already declare it). Only emitted when
    # declared, so undeclared plugins are never falsely badged as local.
    data_locality = meta.get("data_locality", "") or (
        meta.get("suggestion_descriptor") or {}
    ).get("data_locality", "")
    if data_locality:
        entry["data_locality"] = data_locality
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
        "registry_version": "3",
        "repo_url": "https://github.com/asukaonly/magi-plugins.git",
        "plugins": entries,
    }

    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"\nWrote {len(entries)} plugins to {REGISTRY_PATH}")


if __name__ == "__main__":
    main()
