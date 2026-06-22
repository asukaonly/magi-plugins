"""Apple Photos-backed reader for the photo-library plugin."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .reader import (
    ScanResult,
    _collapse_bursts,
    _file_hash_quick,
    _has_retrievable_signal,
    classify_image_type,
)


APPLE_PHOTOS_ASSET_PREFIX = "apple-photos:"
DEFAULT_PHOTOS_LIBRARY_PATH = "~/Pictures/Photos Library.photoslibrary"


class ApplePhotosReaderError(RuntimeError):
    """Base error raised when the Apple Photos reader cannot scan."""


class ApplePhotosUnavailableError(ApplePhotosReaderError):
    """Raised when Apple Photos scanning is unavailable in this environment."""


class ApplePhotosReader:
    """Read image metadata from an Apple Photos library via osxphotos."""

    def __init__(
        self,
        *,
        photosdb_factory: Callable[[str | None], Any] | None = None,
    ) -> None:
        self._photosdb_factory = photosdb_factory

    def scan_library(
        self,
        photos_library_path: str | None = None,
        *,
        limit: int = 500,
        min_modified_at: float = 0.0,
        capture_before: float | None = None,
        order_by: str = "modified_at",
        descending: bool = False,
        include_hidden: bool = False,
    ) -> ScanResult:
        """Scan Apple Photos and return normalized photo items."""
        photosdb = self._open_photosdb(photos_library_path)
        photos = self._read_photos(photosdb)

        candidates: list[dict[str, Any]] = []
        total = 0
        errors = 0
        for photo in photos:
            total += 1
            try:
                item = self._photo_to_item(
                    photo,
                    min_modified_at=min_modified_at,
                    include_hidden=include_hidden,
                    require_signal=True,
                )
            except OSError:
                errors += 1
                continue
            if item is not None:
                if capture_before is not None:
                    capture_ts = float(item.get("capture_timestamp") or 0.0)
                    if capture_ts <= 0 or capture_ts >= float(capture_before):
                        continue
                candidates.append(item)

        sort_field = "capture_timestamp" if order_by == "capture_timestamp" else "modified_at"
        candidates.sort(
            key=lambda item: float(item.get(sort_field) or 0.0),
            reverse=descending,
        )
        has_more = False
        if limit > 0:
            has_more = len(candidates) > limit
            candidates = candidates[:limit]
        collapsed = _collapse_bursts(candidates)
        collapsed.sort(
            key=lambda item: float(item.get(sort_field) or 0.0),
            reverse=descending,
        )
        return ScanResult(
            items=collapsed,
            total_scanned=total,
            errors=errors,
            has_more=has_more,
        )

    def resolve_asset_refs(
        self,
        asset_ref_ids: list[str],
        photos_library_path: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Resolve plugin asset ids back to current local Photos file paths."""
        requested = [str(item or "").strip() for item in asset_ref_ids if str(item or "").strip()]
        if not requested:
            return [], []

        photosdb = self._open_photosdb(photos_library_path)
        uuids = [_strip_asset_prefix(item) for item in requested]
        photos = self._read_photos_by_uuid(photosdb, uuids)

        indexed: dict[str, dict[str, Any]] = {}
        for photo in photos:
            try:
                item = self._photo_to_item(
                    photo,
                    min_modified_at=0.0,
                    include_hidden=True,
                    require_signal=False,
                )
            except OSError:
                continue
            if item is None:
                continue
            indexed[str(item.get("asset_local_id") or "")] = item
            raw_uuid = str(item.get("apple_photos_uuid") or "").strip()
            if raw_uuid:
                indexed[raw_uuid] = item

        resolved: list[dict[str, Any]] = []
        missing: list[str] = []
        for asset_ref_id in requested:
            item = indexed.get(asset_ref_id) or indexed.get(_strip_asset_prefix(asset_ref_id))
            if item is None:
                missing.append(asset_ref_id)
            else:
                resolved.append(item)
        return resolved, missing

    def _open_photosdb(self, photos_library_path: str | None) -> Any:
        if self._photosdb_factory is not None:
            return self._photosdb_factory(_normalize_library_path(photos_library_path))
        if sys.platform != "darwin":
            raise ApplePhotosUnavailableError("Apple Photos source is only available on macOS.")
        try:
            import osxphotos  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ApplePhotosUnavailableError(
                "The osxphotos dependency is not installed for the photo-library plugin."
            ) from exc

        library_path = _normalize_library_path(photos_library_path)
        if library_path:
            return osxphotos.PhotosDB(library_path)
        return osxphotos.PhotosDB()

    @staticmethod
    def _read_photos(photosdb: Any) -> list[Any]:
        try:
            return list(photosdb.photos(images=True, movies=False, intrash=False))
        except TypeError:
            return list(photosdb.photos())

    @staticmethod
    def _read_photos_by_uuid(photosdb: Any, uuids: list[str]) -> list[Any]:
        try:
            return list(photosdb.photos(uuid=uuids, images=True, movies=False, intrash=False))
        except TypeError:
            return [photo for photo in photosdb.photos() if str(getattr(photo, "uuid", "")) in set(uuids)]

    def _photo_to_item(
        self,
        photo: Any,
        *,
        min_modified_at: float,
        include_hidden: bool,
        require_signal: bool,
    ) -> dict[str, Any] | None:
        if not bool(_safe_attr(photo, "isphoto", True)):
            return None
        if not include_hidden and (
            bool(_safe_attr(photo, "hidden", False)) or not bool(_safe_attr(photo, "visible", True))
        ):
            return None

        path_raw = _first_text(_safe_attr(photo, "path", None), _safe_attr(photo, "path_edited", None))
        if not path_raw:
            return None
        path = Path(path_raw).expanduser()
        if not path.is_file():
            return None

        stat = path.stat()
        capture_ts = _datetime_to_timestamp(_safe_attr(photo, "date", None))
        date_modified_ts = _datetime_to_timestamp(_safe_attr(photo, "date_modified", None))
        date_added_ts = _datetime_to_timestamp(_safe_attr(photo, "date_added", None))
        modified_at = max(date_modified_ts, date_added_ts, stat.st_mtime, capture_ts)
        if modified_at <= min_modified_at:
            return None

        exif_info = _safe_attr(photo, "exif_info", None)
        latitude = _first_number(_safe_attr(photo, "latitude", None), _safe_attr(exif_info, "latitude", None))
        longitude = _first_number(_safe_attr(photo, "longitude", None), _safe_attr(exif_info, "longitude", None))
        filename = _first_text(_safe_attr(photo, "original_filename", None), _safe_attr(photo, "filename", None), path.name)
        uuid = str(_safe_attr(photo, "uuid", "") or "").strip()
        place = _safe_attr(photo, "place", None)
        apple_place_name = _first_text(_safe_attr(place, "name", None))
        apple_place_address = _first_text(_safe_attr(place, "address_str", None))
        location_name = _first_text(apple_place_name, apple_place_address)

        item: dict[str, Any] = {
            "asset_local_id": f"{APPLE_PHOTOS_ASSET_PREFIX}{uuid}" if uuid else _file_hash_quick(path),
            "apple_photos_uuid": uuid,
            "source_backend": "apple_photos",
            "path": str(path),
            "filename": filename,
            "extension": path.suffix.lower(),
            "file_size": int(_safe_attr(photo, "original_filesize", 0) or stat.st_size),
            "file_hash": str(_safe_attr(photo, "fingerprint", "") or "") or _file_hash_quick(path),
            "modified_at": modified_at,
            "capture_timestamp": capture_ts or stat.st_mtime,
            "datetime_original": _datetime_to_exif_string(_safe_attr(photo, "date_original", None)),
            "camera_make": _text_attr(exif_info, "camera_make"),
            "camera_model": _text_attr(exif_info, "camera_model"),
            "lens_model": _text_attr(exif_info, "lens_model"),
            "focal_length": _format_focal_length(_safe_attr(exif_info, "focal_length", None)),
            "aperture": _format_aperture(_safe_attr(exif_info, "aperture", None)),
            "exposure_time": _format_exposure_time(_safe_attr(exif_info, "shutter_speed", None)),
            "iso": _format_iso(_safe_attr(exif_info, "iso", None)),
            "image_width": int(_safe_attr(photo, "original_width", 0) or _safe_attr(photo, "width", 0) or 0),
            "image_height": int(_safe_attr(photo, "original_height", 0) or _safe_attr(photo, "height", 0) or 0),
            "orientation": int(_safe_attr(photo, "original_orientation", 0) or _safe_attr(photo, "orientation", 0) or 0),
            "latitude": latitude,
            "longitude": longitude,
            "altitude": None,
            "software": "",
            "favorite": bool(_safe_attr(photo, "favorite", False)),
            "albums": _string_list(_safe_attr(photo, "albums", [])),
            "people": _string_list(_safe_attr(photo, "persons", [])),
            "keywords": _string_list(_safe_attr(photo, "keywords", [])),
            "labels": _string_list(_safe_attr(photo, "labels_normalized", [])),
            "location_name": location_name,
            "location_source": "apple_photos" if location_name else "",
            "apple_photos_place_name": apple_place_name,
            "apple_photos_place_address": apple_place_address,
        }

        if bool(_safe_attr(photo, "screenshot", False)) or classify_image_type(item) == "screenshot":
            return None
        if require_signal and not _has_retrievable_signal(item):
            return None
        return item


def _normalize_library_path(photos_library_path: str | None) -> str | None:
    raw = str(photos_library_path or "").strip()
    if not raw:
        return None
    return str(Path(raw).expanduser())


def _strip_asset_prefix(asset_ref_id: str) -> str:
    value = str(asset_ref_id or "").strip()
    if value.startswith(APPLE_PHOTOS_ASSET_PREFIX):
        return value[len(APPLE_PHOTOS_ASSET_PREFIX):]
    return value


def _safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    try:
        return getattr(obj, name)
    except Exception:
        return default


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _text_attr(obj: Any, name: str) -> str:
    return str(_safe_attr(obj, name, "") or "").strip()


def _first_number(*values: Any) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _datetime_to_timestamp(value: Any) -> float:
    if not isinstance(value, datetime):
        return 0.0
    try:
        return float(value.timestamp())
    except (OSError, OverflowError, ValueError):
        return 0.0


def _datetime_to_exif_string(value: Any) -> str:
    if not isinstance(value, datetime):
        return ""
    return value.strftime("%Y:%m:%d %H:%M:%S")


def _format_focal_length(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{number:.1f}mm" if number > 0 else ""


def _format_aperture(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return f"f/{number:.1f}" if number > 0 else ""


def _format_exposure_time(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    if number < 1:
        return f"1/{round(1 / number)}s"
    return f"{number:.1f}s"


def _format_iso(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    return str(number) if number > 0 else ""
