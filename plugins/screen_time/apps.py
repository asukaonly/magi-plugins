"""Canonical app catalog resolver.

Resolves a raw platform identifier (macOS bundle id or Windows executable path)
to a canonical app id and a localized display name. The catalog is loaded from
``data/apps.json`` bundled with the plugin and may be overlaid with a
user-provided ``apps.local.json`` in the plugin cache directory so users can
extend coverage without forking the plugin.
"""
from __future__ import annotations

import json
import logging
import ntpath
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from magi_plugin_sdk.i18n import get_current_language

logger = logging.getLogger(__name__)

PLATFORM_DARWIN = "darwin"
PLATFORM_WIN32 = "win32"
DEFAULT_LANGUAGE = "en"

_DATA_FILE = Path(__file__).resolve().parent / "data" / "apps.json"
_LOCAL_OVERRIDE_FILENAME = "apps.local.json"


@dataclass(frozen=True)
class ResolvedApp:
    """Result of resolving a raw platform identifier to a canonical app entry."""

    canonical_id: str
    display_name: str
    category: str | None


class _AppCatalog:
    """In-memory lookup tables built from the bundled and user-provided JSON."""

    __slots__ = (
        "_entries_by_id",
        "_macos_bundle_index",
        "_windows_exe_index",
    )

    def __init__(self) -> None:
        self._entries_by_id: dict[str, dict[str, Any]] = {}
        self._macos_bundle_index: dict[str, str] = {}
        self._windows_exe_index: dict[str, str] = {}

    def load_documents(self, documents: Iterable[dict[str, Any]]) -> None:
        for document in documents:
            apps = document.get("apps")
            if not isinstance(apps, list):
                continue
            for entry in apps:
                if not isinstance(entry, dict):
                    continue
                app_id = entry.get("id")
                if not isinstance(app_id, str) or not app_id.strip():
                    continue
                merged = dict(self._entries_by_id.get(app_id, {}))
                merged.update(entry)
                self._entries_by_id[app_id] = merged

                match = entry.get("match")
                if not isinstance(match, dict):
                    continue
                for bundle_id in _string_list(match.get("macos_bundle_id")):
                    self._macos_bundle_index[bundle_id] = app_id
                for exe in _string_list(match.get("windows_exe")):
                    self._windows_exe_index[exe.lower()] = app_id

    def resolve(
        self,
        *,
        platform: str,
        raw_bundle_id: str,
        raw_app_name: str,
    ) -> ResolvedApp:
        """Resolve a raw identifier to a canonical app, falling back gracefully."""
        canonical_id: str | None = None
        entry: dict[str, Any] | None = None

        if platform == PLATFORM_DARWIN:
            canonical_id = self._macos_bundle_index.get(raw_bundle_id)
        elif platform == PLATFORM_WIN32:
            basename = _windows_basename(raw_bundle_id).lower()
            canonical_id = self._windows_exe_index.get(basename)

        if canonical_id is not None:
            entry = self._entries_by_id.get(canonical_id)

        if entry is not None:
            display_name = _pick_display_name(entry.get("name"), raw_app_name)
            category = entry.get("category") if isinstance(entry.get("category"), str) else None
            return ResolvedApp(
                canonical_id=canonical_id or _fallback_canonical_id(platform, raw_bundle_id),
                display_name=display_name,
                category=category,
            )

        fallback_id = _fallback_canonical_id(platform, raw_bundle_id)
        fallback_name = raw_app_name.strip() or _fallback_display_name(platform, raw_bundle_id)
        return ResolvedApp(
            canonical_id=fallback_id,
            display_name=fallback_name,
            category=None,
        )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            trimmed = item.strip()
            if trimmed:
                out.append(trimmed)
    return out


def _pick_display_name(name_field: Any, raw_app_name: str) -> str:
    if isinstance(name_field, dict):
        language = get_current_language()
        for candidate in (language, DEFAULT_LANGUAGE):
            value = name_field.get(candidate)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in name_field.values():
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(name_field, str) and name_field.strip():
        return name_field.strip()
    return raw_app_name.strip() or "Unknown App"


def _fallback_canonical_id(platform: str, raw_bundle_id: str) -> str:
    raw = raw_bundle_id.strip()
    if not raw:
        return f"{platform or 'unknown'}:unknown"
    if platform == PLATFORM_WIN32:
        basename = _windows_basename(raw)
        return f"{platform}:{basename.lower()}" if basename else f"{platform}:{raw.lower()}"
    return f"{platform or 'unknown'}:{raw}"


def _fallback_display_name(platform: str, raw_bundle_id: str) -> str:
    raw = raw_bundle_id.strip()
    if not raw:
        return "Unknown App"
    if platform == PLATFORM_WIN32:
        basename = _windows_basename(raw)
        if basename.lower().endswith(".exe"):
            basename = basename[:-4]
        return basename or raw
    return raw


def _windows_basename(raw_path: str) -> str:
    return ntpath.basename(raw_path) or os.path.basename(raw_path)


_catalog_lock = threading.Lock()
_catalog: _AppCatalog | None = None
_catalog_override_path: Path | None = None


def _build_catalog(override_path: Path | None) -> _AppCatalog:
    catalog = _AppCatalog()
    documents: list[dict[str, Any]] = []
    bundled = _read_json_file(_DATA_FILE)
    if bundled is not None:
        documents.append(bundled)
    if override_path is not None:
        override = _read_json_file(override_path)
        if override is not None:
            documents.append(override)
    catalog.load_documents(documents)
    return catalog


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        logger.warning("Failed to load app catalog %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("App catalog %s is not a JSON object; ignoring", path)
        return None
    return data


def reset_catalog() -> None:
    """Drop the cached catalog so the next resolve rebuilds it (test helper)."""
    global _catalog, _catalog_override_path
    with _catalog_lock:
        _catalog = None
        _catalog_override_path = None


def resolve_app(
    *,
    platform: str,
    raw_bundle_id: str,
    raw_app_name: str,
    override_path: Path | None = None,
) -> ResolvedApp:
    """Resolve a raw foreground-app identifier to a canonical entry.

    Args:
        platform: ``"darwin"`` or ``"win32"``.
        raw_bundle_id: macOS bundle id or Windows executable path.
        raw_app_name: System-provided display name used as a fallback.
        override_path: Optional path to a user-provided ``apps.local.json`` that
            extends or overrides bundled entries. When ``None`` no override is
            applied. The first override path observed is cached; pass
            ``reset_catalog()`` before changing it (mainly useful for tests).
    """
    global _catalog, _catalog_override_path
    with _catalog_lock:
        if _catalog is None or _catalog_override_path != override_path:
            _catalog = _build_catalog(override_path)
            _catalog_override_path = override_path
        catalog = _catalog
    return catalog.resolve(
        platform=platform,
        raw_bundle_id=raw_bundle_id,
        raw_app_name=raw_app_name,
    )


def local_override_path(runtime_paths: Any) -> Path | None:
    """Compute the user-provided override path under the plugin cache directory."""
    plugin_cache_dir = getattr(runtime_paths, "plugin_cache_dir", None)
    if not callable(plugin_cache_dir):
        return None
    try:
        cache_dir = plugin_cache_dir("screen_time")
    except Exception:
        return None
    return Path(cache_dir) / _LOCAL_OVERRIDE_FILENAME
