"""Chat-facing photo tools for the photo-library plugin."""
from __future__ import annotations

from pathlib import Path
import time as _time
from typing import Any

from magi_plugin_sdk.tools import (
    ParameterType,
    Tool,
    ToolErrorCode,
    ToolExecutionContext,
    ToolParameter,
    ToolResult,
    ToolSchema,
)

from .reader import PhotoLibraryReader
from .geocoder import batch_lookup as _geo_batch_lookup, format_location
from .locale_data import get_locale_map


def build_photo_library_tool_classes(settings: dict[str, Any]) -> list[type[Tool]]:
    """Build configured tool classes for the current plugin settings."""
    source_paths = _resolve_source_paths(settings)
    exclude_patterns = _resolve_string_list(settings.get("exclude_patterns"))
    analysis_features = _resolve_string_list(settings.get("analysis_features")) or ["exif"]

    class PhotoLibraryFindCandidatePhotosTool(Tool):
        _source_paths = list(source_paths)
        _exclude_patterns = list(exclude_patterns)
        _analysis_features = list(analysis_features)
        _reader_factory = PhotoLibraryReader

        def __init__(self) -> None:
            self._reader = self._reader_factory()
            super().__init__()

        def _init_schema(self) -> None:
            self.schema = ToolSchema(
                name="photo_library_find_candidate_photos",
                description=(
                    "Search the configured local photo library for real photo files matching time, "
                    "location, or device filters, and return reusable candidate_photo_refs for follow-up sending."
                ),
                category="photos",
                parameters=[
                    ToolParameter(
                        name="start_timestamp",
                        type=ParameterType.FLOAT,
                        description="Inclusive Unix timestamp lower bound for photo capture time.",
                    ),
                    ToolParameter(
                        name="end_timestamp",
                        type=ParameterType.FLOAT,
                        description="Inclusive Unix timestamp upper bound for photo capture time.",
                    ),
                    ToolParameter(
                        name="year",
                        type=ParameterType.INTEGER,
                        description="Calendar year filter in the photo's local capture time, for example 2022.",
                    ),
                    ToolParameter(
                        name="month",
                        type=ParameterType.INTEGER,
                        description="Calendar month filter in the photo's local capture time, from 1 to 12.",
                        min_value=1,
                        max_value=12,
                    ),
                    ToolParameter(
                        name="day",
                        type=ParameterType.INTEGER,
                        description="Calendar day-of-month filter in the photo's local capture time, from 1 to 31.",
                        min_value=1,
                        max_value=31,
                    ),
                    ToolParameter(
                        name="date",
                        type=ParameterType.STRING,
                        description="Explicit local capture date in YYYY-MM-DD format.",
                    ),
                    ToolParameter(
                        name="location_query",
                        type=ParameterType.STRING,
                        description="Case-insensitive substring match against the geocoded location name.",
                    ),
                    ToolParameter(
                        name="device_query",
                        type=ParameterType.STRING,
                        description="Case-insensitive substring match against the camera make/model.",
                    ),
                    ToolParameter(
                        name="limit",
                        type=ParameterType.INTEGER,
                        description="Maximum number of candidate photos to return.",
                        default=6,
                        min_value=1,
                        max_value=100,
                    ),
                ],
            )

        async def validate_parameters(
            self,
            parameters: dict[str, Any],
        ) -> tuple[bool, str | None]:
            _coerce_numeric_parameter(parameters, "start_timestamp", float)
            _coerce_numeric_parameter(parameters, "end_timestamp", float)
            _coerce_numeric_parameter(parameters, "year", int)
            _coerce_numeric_parameter(parameters, "month", int)
            _coerce_numeric_parameter(parameters, "day", int)
            _coerce_numeric_parameter(parameters, "limit", int)

            valid, error = await super().validate_parameters(parameters)
            if not valid:
                return valid, error

            if "date" in parameters and parameters.get("date") not in (None, ""):
                if not _normalize_date(parameters.get("date")):
                    return False, "Parameter date must be in YYYY-MM-DD format"
            return True, None

        async def execute(
            self,
            parameters: dict[str, Any],
            context: ToolExecutionContext,
        ) -> ToolResult:
            if not self._source_paths:
                return ToolResult(
                    success=False,
                    error="photo_library source_paths are not configured.",
                    error_code=ToolErrorCode.INVALID_CONFIG.value,
                )

            limit = _coerce_limit(parameters.get("limit"), default=6, minimum=1, maximum=100)
            start_timestamp = _coerce_float(parameters.get("start_timestamp"))
            end_timestamp = _coerce_float(parameters.get("end_timestamp"))
            year = _coerce_int(parameters.get("year"))
            month = _coerce_int(parameters.get("month"))
            day = _coerce_int(parameters.get("day"))
            date_filter = _normalize_date(parameters.get("date"))
            location_query = _normalize_query(parameters.get("location_query"))
            device_query = _normalize_query(parameters.get("device_query"))

            items = _scan_photo_items(
                reader=self._reader,
                source_paths=self._source_paths,
                exclude_patterns=self._exclude_patterns,
                analysis_features=self._analysis_features,
                min_modified_at=start_timestamp or 0.0,
                max_scan_items=max(limit * 50, 500),
            )
            if location_query:
                _apply_location_labels(
                    items,
                    locale=str(context.env_vars.get("locale") or "").strip(),
                )
            matches = _filter_photo_items(
                items,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                year=year,
                month=month,
                day=day,
                date_filter=date_filter,
                location_query=location_query,
                device_query=device_query,
            )
            matches.sort(
                key=lambda item: float(item.get("capture_timestamp") or item.get("modified_at") or 0.0),
                reverse=True,
            )
            selected = matches[:limit]

            candidate_photo_refs = [_build_photo_ref(item) for item in selected]
            previews = [_build_photo_preview(item) for item in selected]

            return ToolResult(
                success=True,
                data={
                    "candidate_photo_refs": candidate_photo_refs,
                    "assistant_payload": {"candidate_photo_refs": candidate_photo_refs},
                    "candidate_count": len(candidate_photo_refs),
                    "matched_photos": previews,
                    "summary": _build_search_summary(
                        candidate_count=len(candidate_photo_refs),
                        start_timestamp=start_timestamp,
                        end_timestamp=end_timestamp,
                        year=year,
                        month=month,
                        day=day,
                        date_filter=date_filter,
                        location_query=location_query,
                        device_query=device_query,
                    ),
                },
            )

    class PhotoLibraryResolvePhotoRefsTool(Tool):
        _source_paths = list(source_paths)
        _exclude_patterns = list(exclude_patterns)
        _analysis_features = list(analysis_features)
        _reader_factory = PhotoLibraryReader

        def __init__(self) -> None:
            self._reader = self._reader_factory()
            super().__init__()

        def _init_schema(self) -> None:
            self.schema = ToolSchema(
                name="photo_library_resolve_photo_refs",
                description=(
                    "Resolve previously returned photo_ref_ids back to current local file paths so the host can "
                    "prepare chat attachments for sending."
                ),
                category="photos",
                parameters=[
                    ToolParameter(
                        name="photo_ref_ids",
                        type=ParameterType.ARRAY,
                        required=True,
                        array_item_type=ParameterType.STRING,
                        description="Photo reference ids previously returned by photo_library_find_candidate_photos.",
                    ),
                ],
            )

        async def execute(
            self,
            parameters: dict[str, Any],
            context: ToolExecutionContext,
        ) -> ToolResult:
            _ = context
            if not self._source_paths:
                return ToolResult(
                    success=False,
                    error="photo_library source_paths are not configured.",
                    error_code=ToolErrorCode.INVALID_CONFIG.value,
                )

            photo_ref_ids = parameters.get("photo_ref_ids")
            if not isinstance(photo_ref_ids, list) or not photo_ref_ids:
                return ToolResult(
                    success=False,
                    error="photo_ref_ids must be a non-empty list.",
                    error_code=ToolErrorCode.INVALID_PARAMETERS.value,
                )

            requested_ids = [str(item or "").strip() for item in photo_ref_ids if str(item or "").strip()]
            if not requested_ids:
                return ToolResult(
                    success=False,
                    error="photo_ref_ids must contain at least one non-empty id.",
                    error_code=ToolErrorCode.INVALID_PARAMETERS.value,
                )

            items = _scan_photo_items(
                reader=self._reader,
                source_paths=self._source_paths,
                exclude_patterns=self._exclude_patterns,
                analysis_features=self._analysis_features,
                min_modified_at=0.0,
                max_scan_items=max(len(requested_ids) * 200, 1000),
            )
            indexed = {_photo_ref_id(item): item for item in items}

            resolved_items: list[dict[str, Any]] = []
            missing_ids: list[str] = []
            for photo_ref_id in requested_ids:
                item = indexed.get(photo_ref_id)
                if item is None:
                    missing_ids.append(photo_ref_id)
                    continue
                resolved_items.append(item)

            resolved_refs = [_build_photo_ref(item) for item in resolved_items]
            file_paths = [str(item.get("path") or "") for item in resolved_items if str(item.get("path") or "")]

            return ToolResult(
                success=True,
                data={
                    "photo_refs": resolved_refs,
                    "assistant_payload": {"photo_refs": resolved_refs},
                    "file_paths": file_paths,
                    "resolved_count": len(file_paths),
                    "missing_photo_ref_ids": missing_ids,
                    "summary": (
                        f"Resolved {len(file_paths)} photo file(s). "
                        "Call prepare_chat_attachments with file_paths to send them in chat."
                    ),
                },
            )

    return [PhotoLibraryFindCandidatePhotosTool, PhotoLibraryResolvePhotoRefsTool]


def _resolve_source_paths(settings: dict[str, Any]) -> list[str]:
    raw_paths = settings.get("source_paths")
    if isinstance(raw_paths, list):
        return [str(item) for item in raw_paths if str(item or "").strip()]
    legacy = settings.get("source_path")
    if str(legacy or "").strip():
        return [str(legacy).strip()]
    return []


def _resolve_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _coerce_numeric_parameter(
    parameters: dict[str, Any],
    name: str,
    cast_type: type[int] | type[float],
) -> None:
    if name not in parameters:
        return
    value = parameters.get(name)
    if value is None or value == "" or isinstance(value, cast_type):
        return
    if isinstance(value, bool):
        return
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return
        try:
            parameters[name] = cast_type(raw)
        except ValueError:
            return


def _coerce_limit(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_query(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) != 10:
        return ""
    year, sep1, month, sep2, day = text[:4], text[4], text[5:7], text[7], text[8:10]
    if sep1 != "-" or sep2 != "-":
        return ""
    if not (year.isdigit() and month.isdigit() and day.isdigit()):
        return ""
    return text


def _scan_photo_items(
    *,
    reader: PhotoLibraryReader,
    source_paths: list[str],
    exclude_patterns: list[str],
    analysis_features: list[str],
    min_modified_at: float,
    max_scan_items: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    scan_limit = max(100, max_scan_items)
    for source_path in source_paths:
        result = reader.scan_directory(
            source_path,
            limit=scan_limit,
            min_modified_at=min_modified_at,
            exclude_patterns=exclude_patterns,
            analysis_features=analysis_features,
        )
        allowed_root = Path(source_path).expanduser().resolve()
        for item in result.items:
            item_path_raw = str(item.get("path") or "").strip()
            if not item_path_raw:
                continue
            try:
                item_path = Path(item_path_raw).expanduser().resolve()
            except OSError:
                continue
            if allowed_root in {item_path, *item_path.parents}:
                items.append(dict(item))
    return items


def _filter_photo_items(
    items: list[dict[str, Any]],
    *,
    start_timestamp: float | None,
    end_timestamp: float | None,
    year: int | None,
    month: int | None,
    day: int | None,
    date_filter: str,
    location_query: str,
    device_query: str,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for item in items:
        capture_ts = float(item.get("capture_timestamp") or item.get("modified_at") or 0.0)
        if start_timestamp is not None and capture_ts < start_timestamp:
            continue
        if end_timestamp is not None and capture_ts > end_timestamp:
            continue
        if not _matches_calendar_filter(
            capture_ts,
            year=year,
            month=month,
            day=day,
            date_filter=date_filter,
        ):
            continue
        location_name = str(item.get("location_name") or "").lower()
        if location_query and location_query not in location_name:
            continue
        device_label = " ".join(
            part for part in [str(item.get("camera_make") or ""), str(item.get("camera_model") or "")] if part
        ).lower()
        if device_query and device_query not in device_label:
            continue
        matches.append(item)
    return matches


def _matches_calendar_filter(
    capture_ts: float,
    *,
    year: int | None,
    month: int | None,
    day: int | None,
    date_filter: str,
) -> bool:
    if not any([year is not None, month is not None, day is not None, date_filter]):
        return True
    try:
        local_time = _time.localtime(capture_ts)
    except (OverflowError, OSError, ValueError):
        return False
    if date_filter:
        candidate_date = _time.strftime("%Y-%m-%d", local_time)
        if candidate_date != date_filter:
            return False
    if year is not None and int(local_time.tm_year) != int(year):
        return False
    if month is not None and int(local_time.tm_mon) != int(month):
        return False
    if day is not None and int(local_time.tm_mday) != int(day):
        return False
    return True


def _apply_location_labels(items: list[dict[str, Any]], *, locale: str) -> None:
    coords: list[tuple[float, float]] = []
    indices: list[int] = []
    for index, item in enumerate(items):
        if str(item.get("location_name") or "").strip():
            continue
        latitude = item.get("latitude")
        longitude = item.get("longitude")
        if latitude is None or longitude is None:
            continue
        coords.append((float(latitude), float(longitude)))
        indices.append(index)
    if not coords:
        return
    cache_dir = Path.home() / ".magi" / "cache" / "plugins" / "photo-library"
    locale_map = get_locale_map(locale)
    geo_results = _geo_batch_lookup(coords, cache_dir)
    for index, geo in zip(indices, geo_results):
        if geo is None:
            continue
        items[index]["location_name"] = format_location(geo, locale_map=locale_map)


def _photo_ref_id(item: dict[str, Any]) -> str:
    raw = str(item.get("asset_local_id") or item.get("file_hash") or "").strip()
    if raw:
        return raw
    filename = str(item.get("filename") or "unknown")
    modified_at = int(float(item.get("modified_at") or 0.0))
    return f"fallback:{filename}:{modified_at}"


def _build_photo_ref(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "photo_ref_id": _photo_ref_id(item),
        "source_item_id": str(item.get("asset_local_id") or ""),
        "original_name": str(item.get("filename") or ""),
        "captured_at": float(item.get("capture_timestamp") or item.get("modified_at") or 0.0),
        "kind": "image",
    }


def _build_photo_preview(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "photo_ref_id": _photo_ref_id(item),
        "original_name": str(item.get("filename") or ""),
        "captured_at": float(item.get("capture_timestamp") or item.get("modified_at") or 0.0),
        "location_name": str(item.get("location_name") or ""),
        "device_name": " ".join(
            part for part in [str(item.get("camera_make") or ""), str(item.get("camera_model") or "")] if part
        ).strip(),
    }


def _build_search_summary(
    *,
    candidate_count: int,
    start_timestamp: float | None,
    end_timestamp: float | None,
    year: int | None,
    month: int | None,
    day: int | None,
    date_filter: str,
    location_query: str,
    device_query: str,
) -> str:
    filters: list[str] = []
    if start_timestamp is not None:
        filters.append(f"start>={int(start_timestamp)}")
    if end_timestamp is not None:
        filters.append(f"end<={int(end_timestamp)}")
    if date_filter:
        filters.append(f"date={date_filter}")
    else:
        if year is not None:
            filters.append(f"year={year}")
        if month is not None:
            filters.append(f"month={month}")
        if day is not None:
            filters.append(f"day={day}")
    if location_query:
        filters.append(f"location~{location_query}")
    if device_query:
        filters.append(f"device~{device_query}")
    if not filters:
        return f"Found {candidate_count} candidate photo(s)."
    joined = ", ".join(filters)
    return f"Found {candidate_count} candidate photo(s) matching {joined}."