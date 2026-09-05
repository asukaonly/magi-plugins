#!/usr/bin/env python3
"""Generate registry v4 and immutable package-version history.

Stage every plugin package change before running this command. Generation reads
one frozen snapshot of the staged Git index, never unstaged package content.

Usage:
    python scripts/build-registry.py
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path
from pathlib import PurePosixPath
from typing import Callable
from xml.etree import ElementTree

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
    tracked_plugin_directories,
    tracked_plugin_package_metadata,
    tracked_repository_file_bytes,
    validate_plugin_worktree,
)
from registry_contract import (  # noqa: E402
    REGISTRY_VERSION,
    RegistryContractError,
    validate_plugin_manifest,
    validate_registry_index,
)
from version_history import (  # noqa: E402
    PackageVersionRecord,
    VersionHistoryError,
    assert_current_version_is_latest,
    assert_history_extends,
    bind_package_version,
    load_version_history,
    package_version_key,
    parse_version_history,
    write_version_history,
)

REPO_ROOT = SCRIPT_DIR.parent
REGISTRY_PATH = REPO_ROOT / "registry.json"
OFFICIAL_ALLOWLIST_PATH = REPO_ROOT / "official-plugins.json"
VERSION_HISTORY_PATH = REPO_ROOT / "version-history.json"

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


def load_official_ids(
    *,
    content_reader: Callable[[Path], bytes] | None = None,
) -> set[str]:
    """Maintainer-controlled set of plugin_ids allowed to be `official`.

    Authority for the `official` flag lives here, NOT in each plugin's
    plugin.toml — a third-party PR touching only plugins/<their-plugin>/
    cannot grant itself official status.
    """
    if content_reader is None and not OFFICIAL_ALLOWLIST_PATH.exists():
        print(
            f"note: {OFFICIAL_ALLOWLIST_PATH.name} not found; all plugins "
            f"marked non-official",
            file=sys.stderr,
        )
        return set()
    try:
        if content_reader is None:
            with open(OFFICIAL_ALLOWLIST_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        else:
            data = json.loads(content_reader(OFFICIAL_ALLOWLIST_PATH).decode("utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"error: {OFFICIAL_ALLOWLIST_PATH.name} is not valid JSON: {exc}")
    return set(data.get("official_plugin_ids", []))


def _asset_icon_path(
    plugin_dir: Path,
    icon: str,
    *,
    validate_filesystem: bool = True,
) -> Path | None:
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
    candidate = plugin_dir / Path(*relative.parts)
    if not validate_filesystem:
        return candidate
    root = plugin_dir.resolve()
    if candidate.is_symlink():
        raise ValueError(f"Plugin icon asset cannot be a symlink: {icon}")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Plugin icon asset escapes package directory: {icon}")
    return resolved


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


def encode_icon_asset(
    plugin_dir: Path,
    icon: str,
    *,
    content_reader: Callable[[Path], bytes] | None = None,
) -> str | None:
    path = _asset_icon_path(
        plugin_dir,
        icon,
        validate_filesystem=content_reader is None,
    )
    if path is None:
        return None
    if content_reader is None and not path.is_file():
        raise ValueError(f"Plugin icon asset does not exist: {path}")
    suffix = path.suffix.lower()
    mime_type = ICON_MIME_TYPES.get(suffix)
    if mime_type is None:
        raise ValueError(f"Unsupported plugin icon format: {path}")
    data = content_reader(path) if content_reader is not None else path.read_bytes()
    if not data or len(data) > MAX_ICON_BYTES:
        raise ValueError(
            f"Plugin icon must be between 1 and {MAX_ICON_BYTES} bytes: {path}"
        )
    if suffix == ".svg":
        _validate_svg_icon(data, path=path)
    elif suffix == ".png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"Plugin icon is not a valid PNG: {path}")
    elif suffix == ".webp" and not (data.startswith(b"RIFF") and data[8:12] == b"WEBP"):
        raise ValueError(f"Plugin icon is not a valid WebP image: {path}")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def build_entry(
    plugin_dir: Path,
    official_ids: set[str],
    *,
    content_reader: Callable[[Path], bytes] | None = None,
) -> dict | None:
    toml_path = plugin_dir / "plugin.toml"
    if content_reader is None and not toml_path.exists():
        return None
    if content_reader is None:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
    else:
        data = tomllib.loads(content_reader(toml_path).decode("utf-8"))
    meta = data.get("plugin", {})
    validate_plugin_manifest(meta, package_name=plugin_dir.name)
    plugin_id = meta.get("id", plugin_dir.name)
    version = meta.get("version", "0.0.0")
    package_version_key(plugin_id, version)
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
        icon_data = encode_icon_asset(
            plugin_dir,
            icon,
            content_reader=content_reader,
        )
        if icon_data is not None:
            entry["icon_data"] = icon_data
    entry["version"] = version
    for field in ("protocol_version", "min_sdk_version", "execution_mode", "projection_sources", "settings_fields", "settings_actions", "settings_resources", "settings_ui_blocks"):
        entry[field] = meta.get(field, []) if field.startswith("settings_") else meta[field]
    if meta.get("activation_flow") is not None:
        entry["activation_flow"] = meta["activation_flow"]
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
    suggestion_descriptor = meta.get("suggestion_descriptor")
    if suggestion_descriptor is not None:
        entry["suggestion_descriptor"] = suggestion_descriptor
    return entry


def _committed_version_history() -> dict[str, PackageVersionRecord]:
    """Read the immutable history from HEAD, before staged publication changes."""

    head_check = subprocess.run(
        ["git", "cat-file", "-e", "HEAD^{commit}"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if head_check.returncode != 0:
        return {}
    history_check = subprocess.run(
        ["git", "cat-file", "-e", "HEAD:version-history.json"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if history_check.returncode != 0:
        return {}
    result = subprocess.run(
        ["git", "show", "HEAD:version-history.json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise VersionHistoryError(
            f"Cannot read HEAD:version-history.json: {result.stderr.strip()}"
        )
    return parse_version_history(
        result.stdout,
        label="HEAD:version-history.json",
    )


def _prepare_version_history(
    committed: dict[str, PackageVersionRecord],
    candidate: dict[str, PackageVersionRecord],
) -> dict[str, PackageVersionRecord]:
    """Preserve committed identities and recalculate every staged publication."""

    assert_history_extends(
        committed,
        candidate,
        base_label="HEAD:version-history.json",
    )
    return dict(committed)


def main() -> None:
    validate_plugin_worktree(REPO_ROOT)
    tree_id = snapshot_git_index(REPO_ROOT)

    def content_reader(path: Path) -> bytes:
        return tracked_repository_file_bytes(REPO_ROOT, path, tree_id=tree_id)

    official_ids = load_official_ids(content_reader=content_reader)
    version_history = _prepare_version_history(
        _committed_version_history(),
        load_version_history(VERSION_HISTORY_PATH),
    )
    entries = []

    for child in tracked_plugin_directories(REPO_ROOT, tree_id):
        publication_metadata = tracked_plugin_package_metadata(
            REPO_ROOT,
            child,
            tree_id=tree_id,
        )
        entry = build_entry(
            child,
            official_ids,
            content_reader=content_reader,
        )
        if entry:
            entry["package_sha256"] = publication_metadata.package_sha256
            assert_current_version_is_latest(
                version_history,
                plugin_id=entry["plugin_id"],
                version=entry["version"],
            )
            bind_package_version(
                version_history,
                plugin_id=entry["plugin_id"],
                version=entry["version"],
                package_sha256=publication_metadata.package_sha256,
                executable_paths=publication_metadata.executable_paths,
            )
            entries.append(entry)
            print(f"  + {entry['plugin_id']} v{entry['version']}")

    registry = {
        "registry_version": REGISTRY_VERSION,
        "repo_url": "https://github.com/asukaonly/magi-plugins.git",
        "plugins": entries,
    }
    validate_registry_index(registry)

    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)
        f.write("\n")
    write_version_history(VERSION_HISTORY_PATH, version_history)

    print(f"\nWrote {len(entries)} plugins to {REGISTRY_PATH}")


if __name__ == "__main__":
    try:
        main()
    except (
        PackageIdentityError,
        RegistryContractError,
        VersionHistoryError,
        ValueError,
    ) as exc:
        raise SystemExit(f"error: {exc}") from exc
