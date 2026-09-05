"""Timeline source shared by local and Apple photo plugins.

Emits one L1 event per *photo session* 鈥?a coherent shooting activity
defined by (local date, device, geo cell) with cross-midnight merging.
Per-photo records are not surfaced individually: a typical user shoots
hundreds of photos that compress into a handful of memorable sessions,
and that is the unit the memory layer should index.
"""

from __future__ import annotations

from magi_plugin_sdk.runtime import SourceChange, SourceChangeBatch

import asyncio
import json
import time as _time
from pathlib import Path
from typing import Any

from magi_plugin_sdk import UserContentClearContext
from magi_plugin_sdk.sources import (
    ContentBlock,
    L2BatchPolicy,
    Source,
    SourceMemoryPolicy,
    SourceOutput,
    SourceOutputMetadata,
    SourceSyncContext,
)

from .geocoder import batch_lookup as _geo_batch_lookup, format_location
from .apple_photos_reader import (
    DEFAULT_PHOTOS_LIBRARY_PATH,
    ApplePhotosReader,
    ApplePhotosReaderError,
)
from .file_index import FileIndexCache
from .locale_data import get_locale_map
from .normalizers import (
    build_session_entity_hints,
    build_session_fact_hints,
    build_session_relation_candidates,
    build_session_retrieval_terms,
    build_session_source_facets,
)
from .reader import PhotoLibraryReader
from .sessions import aggregate_sessions

# Sessions with no new captures within this window are emitted to L1.
# Younger sessions are deferred so each session is written exactly once
# with its full content; the host owns versioned ingestion receipts.
_DEFAULT_SETTLE_WINDOW_SECONDS = 4 * 3600
_APPLE_PHOTOS_CURSOR_VERSION = 1


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _decode_apple_photos_cursor(raw_cursor: str | None) -> dict[str, Any]:
    if not raw_cursor:
        return {
            "version": _APPLE_PHOTOS_CURSOR_VERSION,
            "mode": "backfill",
            "capture_before": None,
            "modified_since": 0.0,
        }
    data = json.loads(raw_cursor)
    if not isinstance(data, dict) or data.get("version") != _APPLE_PHOTOS_CURSOR_VERSION:
        raise ValueError("Unsupported Apple Photos source cursor")
    mode = data.get("mode")
    if mode not in {"backfill", "incremental"}:
        raise ValueError("Invalid Apple Photos source cursor mode")
    capture_before = data.get("capture_before")
    return {
        "version": _APPLE_PHOTOS_CURSOR_VERSION,
        "mode": mode,
        "capture_before": _float_value(capture_before) if capture_before is not None else None,
        "modified_since": _float_value(data.get("modified_since")),
    }


def _encode_apple_photos_cursor(
    *,
    mode: str,
    capture_before: float | None,
    modified_since: float,
) -> str:
    payload: dict[str, Any] = {
        "version": _APPLE_PHOTOS_CURSOR_VERSION,
        "mode": mode,
        "modified_since": float(modified_since),
    }
    if capture_before is not None:
        payload["capture_before"] = float(capture_before)
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _parse_local_date_start(value: Any) -> float | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        parsed = _time.strptime(normalized, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return float(_time.mktime(parsed))


def _resolve_custom_capture_bounds(source_settings: dict[str, Any]) -> tuple[float | None, float | None]:
    if str(source_settings.get("initial_sync_policy") or "") != "custom_range":
        return None, None
    start_ts = _parse_local_date_start(source_settings.get("initial_sync_start_date"))
    end_ts = _parse_local_date_start(source_settings.get("initial_sync_end_date"))
    if start_ts is None or end_ts is None or end_ts < start_ts:
        return None, None
    return start_ts, end_ts + 24 * 60 * 60


class PhotoLibraryTimelineSource(Source):
    """Pull-sync source that aggregates a local photo directory into sessions."""

    source_id = "timeline.photo_library"
    display_name = "Photo Library"
    source_type = "photo_library"
    polling_mode = "interval"
    default_interval = 60
    update_key_fields = ("session_key",)
    relation_edge_whitelist = ("OWNS", "VISITED")
    supports_pull_sync = True

    memory_policy = SourceMemoryPolicy(
        retention_class="compressible",
        cognition_eligible=True,
        importance_bias=0.6,
    )

    def __init__(
        self,
        *,
        source_paths: list[str] | None = None,
        source_mode: str = "directory",
        photos_library_path: str = DEFAULT_PHOTOS_LIBRARY_PATH,
        max_items_per_sync: int = 200,
        analysis_features: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        settle_window_seconds: float = _DEFAULT_SETTLE_WINDOW_SECONDS,
        reader: PhotoLibraryReader | None = None,
        apple_reader: ApplePhotosReader | None = None,
        source_id: str | None = None,
        source_type: str | None = None,
        display_name: str | None = None,
    ) -> None:
        super().__init__()
        if source_id:
            self.source_id = source_id
        if source_type:
            self.source_type = source_type
        if display_name:
            self.display_name = display_name
        self.source_paths = source_paths or []
        self.source_mode = source_mode
        self.photos_library_path = photos_library_path
        self.max_items_per_sync = max_items_per_sync
        self.analysis_features = analysis_features or ["exif"]
        self.exclude_patterns = exclude_patterns or []
        self.settle_window_seconds = settle_window_seconds
        self._reader = reader or PhotoLibraryReader()
        self._apple_reader = apple_reader or ApplePhotosReader()
        self._operation_lock = asyncio.Lock()

    async def clear_user_content(self, context: UserContentClearContext) -> None:
        """Delete the local photo metadata index without touching source state."""

        async with self._operation_lock:
            current_index = getattr(self._reader, "_file_index", None)
            if isinstance(current_index, FileIndexCache):
                await asyncio.to_thread(current_index.close)
            self._reader._file_index = None

            cache_dir = context.runtime_paths.plugin_cache_dir(self.source_type)
            await asyncio.to_thread(FileIndexCache(cache_dir).clear_user_content)

    # ------------------------------------------------------------------
    # Identity & dedup
    # ------------------------------------------------------------------

    def source_item_identity(self, item: dict[str, Any]) -> str:
        return str(item.get("session_key") or "session:unknown")


    # ------------------------------------------------------------------
    # L2 batching
    # ------------------------------------------------------------------

    def l2_batch_policy(self, output: SourceOutput) -> L2BatchPolicy | None:
        """Group session events by year-month for higher-level synthesis."""
        ts = output.occurred_at or output.captured_at or _time.time()
        try:
            month = _time.strftime("%Y%m", _time.localtime(ts))
        except (OSError, OverflowError, ValueError):
            month = "unknown"
        owner = f"{self.source_type}:{month}"
        return L2BatchPolicy(
            owner=owner,
            catch_up_owner=f"{self.source_type}:catchup",
            max_events=15,
            min_ready_events=2,
            max_wait_seconds=600,
        )

    # ------------------------------------------------------------------
    # Pull-sync
    # ------------------------------------------------------------------

    async def collect_items(self, context: SourceSyncContext) -> SourceChangeBatch:
        async with self._operation_lock:
            return await self._collect_items(context)

    async def _collect_items(self, context: SourceSyncContext) -> SourceChangeBatch:
        source_settings = (
            context.plugin_settings.get("sources", {}).get(self.source_type, {})
            if isinstance(context.plugin_settings.get("sources", {}), dict)
            else {}
        )
        source_mode = str(
            source_settings.get("source_mode", self.source_mode) or "directory"
        ).strip()
        if source_mode not in {"directory", "apple_photos"}:
            source_mode = "directory"

        # Resolve source paths: prefer settings list, fall back to instance.
        source_paths: list[str] = []
        raw_paths = source_settings.get("source_paths")
        if isinstance(raw_paths, list):
            source_paths = [str(p) for p in raw_paths if p]
        if not source_paths:
            source_paths = list(self.source_paths)

        if source_mode == "directory" and not source_paths:
            return SourceChangeBatch(
                changes=[],
                stats={"count": 0, "error": "source_paths not configured"},
            )

        raw_excludes = source_settings.get("exclude_patterns")
        exclude_patterns = (
            [str(p) for p in raw_excludes if p]
            if isinstance(raw_excludes, list)
            else list(self.exclude_patterns)
        )

        # The directory cursor stores the max modified_at of photos that have
        # already contributed to a settled session. Apple Photos uses an opaque
        # cursor during capture-time history backfill, then switches back to a
        # modified_at-based incremental cursor.
        now_ts = _time.time()
        custom_capture_after, custom_capture_before = _resolve_custom_capture_bounds(source_settings)
        apple_custom_backfill = bool(
            source_mode == "apple_photos"
            and custom_capture_after is not None
            and custom_capture_before is not None
        )
        apple_cursor = (
            _decode_apple_photos_cursor(context.last_cursor)
            if source_mode == "apple_photos"
            else None
        )
        apple_backfill = bool(
            source_mode == "apple_photos"
            and apple_cursor is not None
            and (apple_cursor.get("mode") == "backfill" or apple_custom_backfill)
        )
        last_cursor = 0.0
        if source_mode == "apple_photos" and apple_cursor is not None:
            last_cursor = _float_value(apple_cursor.get("modified_since"))
        elif context.last_cursor:
            last_cursor = _float_value(context.last_cursor)
        look_back = now_ts - self.settle_window_seconds * 2
        min_modified_at = max(0.0, min(last_cursor, look_back))

        # The reader's per-scan limit caps photos, not sessions.
        photo_limit = max(self.max_items_per_sync, 1000)

        analysis_features = list(source_settings.get("analysis_features", self.analysis_features))

        all_photos: list[dict[str, Any]] = []
        total_scanned = 0
        total_errors = 0
        source_has_more = False
        source_error = ""

        if source_mode == "apple_photos":
            photos_library_path = str(
                source_settings.get("photos_library_path", self.photos_library_path) or ""
            ).strip()
            apple_capture_before = (
                apple_cursor.get("capture_before")
                if apple_backfill and apple_cursor is not None
                else None
            )
            if apple_custom_backfill and custom_capture_before is not None:
                if apple_capture_before is None:
                    apple_capture_before = custom_capture_before
                else:
                    apple_capture_before = min(float(apple_capture_before), custom_capture_before)
            try:
                result = await asyncio.to_thread(
                    self._apple_reader.scan_library,
                    photos_library_path,
                    limit=photo_limit,
                    min_modified_at=0.0 if apple_backfill else min_modified_at,
                    capture_after=custom_capture_after if apple_custom_backfill else None,
                    capture_before=apple_capture_before,
                    order_by="capture_timestamp" if apple_backfill else "modified_at",
                    descending=apple_backfill,
                )
                total_scanned += result.total_scanned
                total_errors += result.errors
                source_has_more = source_has_more or bool(getattr(result, "has_more", False))
                all_photos.extend(result.items)
            except ApplePhotosReaderError as exc:
                source_error = str(exc)
        else:
            file_index: FileIndexCache | None = None
            if "exif" in analysis_features:
                try:
                    cache_dir = context.runtime_paths.plugin_cache_dir(self.source_type)
                    file_index = FileIndexCache(cache_dir)
                except Exception:
                    file_index = None
            if file_index is not None:
                self._reader._file_index = file_index

            for src in source_paths:
                result = await asyncio.to_thread(
                    self._reader.scan_directory,
                    src,
                    limit=photo_limit,
                    min_modified_at=min_modified_at,
                    exclude_patterns=exclude_patterns,
                    analysis_features=analysis_features,
                )
                total_scanned += result.total_scanned
                total_errors += result.errors
                source_has_more = source_has_more or bool(getattr(result, "has_more", False))

                allowed_root = Path(src).expanduser().resolve()
                for item in result.items:
                    item_path = Path(str(item.get("path", ""))).resolve()
                    if allowed_root in {item_path, *item_path.parents}:
                        all_photos.append(item)

        if source_error:
            return SourceChangeBatch(
                changes=[],
                stats={
                    "count": 0,
                    "photos_seen": 0,
                    "total_scanned": total_scanned,
                    "errors": total_errors + 1,
                    "source_mode": source_mode,
                    "error": source_error,
                },
            )

        # Reverse-geocode before session aggregation so location_name
        # participates in the session's representative pick.
        if "geocode" in analysis_features and all_photos:
            cache_dir = context.runtime_paths.plugin_cache_dir(self.source_type)
            locale_map = get_locale_map(str(context.plugin_settings.get("locale", "")))
            coords = [
                (float(p["latitude"]), float(p["longitude"]))
                for p in all_photos
                if p.get("latitude") is not None and p.get("longitude") is not None
            ]
            indices = [
                i
                for i, p in enumerate(all_photos)
                if p.get("latitude") is not None and p.get("longitude") is not None
            ]
            if coords:
                geo_results = await asyncio.to_thread(_geo_batch_lookup, coords, cache_dir)
                for idx, geo in zip(indices, geo_results):
                    has_source_location = bool(
                        str(all_photos[idx].get("location_name") or "").strip()
                    )
                    if geo is not None and not has_source_location:
                        all_photos[idx]["location_name"] = format_location(
                            geo, locale_map=locale_map
                        )
                        all_photos[idx]["location_source"] = "geocode"
                        all_photos[idx]["location_country"] = geo.country_code

        sessions, max_settled_mtime = aggregate_sessions(
            all_photos,
            now_ts=now_ts,
            settle_window_seconds=self.settle_window_seconds,
        )
        if apple_backfill:
            sessions.sort(key=lambda s: float(s.get("first_capture_ts") or 0.0), reverse=True)

        # Cap emission per sync to keep individual L2 batches manageable.
        sessions_capped = False
        if len(sessions) > self.max_items_per_sync:
            sessions_capped = True
            sessions = sessions[: self.max_items_per_sync]
            max_settled_mtime = max(float(s.get("max_modified_at") or 0.0) for s in sessions)

        # Cursor only advances past photos that contributed to an emitted
        # session. Unsettled recent photos stay within the look-back window.
        next_cursor = context.last_cursor
        watermark_ts = context.last_success_at
        max_seen_mtime = max(
            [last_cursor] + [float(p.get("modified_at") or 0.0) for p in all_photos]
        )
        has_more = bool(source_has_more or sessions_capped)
        if source_mode == "apple_photos" and apple_cursor is not None:
            if apple_backfill:
                capture_candidates = [
                    float(s.get("first_capture_ts") or 0.0)
                    for s in sessions
                    if float(s.get("first_capture_ts") or 0.0) > 0
                ]
                if not capture_candidates:
                    capture_candidates = [
                        float(p.get("capture_timestamp") or 0.0)
                        for p in all_photos
                        if float(p.get("capture_timestamp") or 0.0) > 0
                    ]
                capture_before = min(capture_candidates) if capture_candidates else None
                can_continue_backfill = has_more and capture_before is not None
                if (
                    can_continue_backfill
                    and apple_custom_backfill
                    and custom_capture_after is not None
                    and capture_before <= custom_capture_after
                ):
                    can_continue_backfill = False
                if can_continue_backfill and capture_before is not None:
                    next_cursor = _encode_apple_photos_cursor(
                        mode="backfill",
                        capture_before=capture_before,
                        modified_since=max_seen_mtime,
                    )
                    watermark_ts = max_seen_mtime or context.last_success_at
                else:
                    modified_since = max(max_seen_mtime, max_settled_mtime, now_ts)
                    next_cursor = _encode_apple_photos_cursor(
                        mode="incremental",
                        capture_before=None,
                        modified_since=modified_since,
                    )
                    watermark_ts = modified_since
            else:
                modified_since = max(max_seen_mtime, max_settled_mtime)
                if modified_since > 0:
                    next_cursor = _encode_apple_photos_cursor(
                        mode="incremental",
                        capture_before=None,
                        modified_since=modified_since,
                    )
                    watermark_ts = modified_since
        elif max_settled_mtime > 0:
            next_cursor = str(max_settled_mtime)
            watermark_ts = max_settled_mtime

        stats: dict[str, Any] = {
            "count": len(sessions),
            "photos_seen": len(all_photos),
            "total_scanned": total_scanned,
            "errors": total_errors,
            "source_mode": source_mode,
            "has_more": has_more,
        }
        if source_mode == "apple_photos":
            stats["cursor_kind"] = "opaque"
            stats["sync_phase"] = "backfill" if apple_backfill else "incremental"

        return SourceChangeBatch(
            changes=[SourceChange(object_id=self.source_item_identity(item), version=self.source_item_version_fingerprint(item), payload=item) for item in sessions],
            next_cursor=next_cursor,
            watermark_ts=watermark_ts,
            complete=not has_more,
            stats=stats,
        )

    # ------------------------------------------------------------------
    # Output building
    # ------------------------------------------------------------------

    async def fetch_item(self, item: dict[str, Any]) -> dict[str, Any]:
        return dict(item)

    async def build_output(self, item: dict[str, Any]) -> SourceOutput:
        device = str(item.get("device_name") or "").strip()
        location = str(item.get("location_name") or "").strip()
        date = str(item.get("date") or "")
        weekday_index = int(
            item.get("weekday_index") if item.get("weekday_index") is not None else -1
        )
        time_of_day_key = str(item.get("time_of_day") or "")
        photo_count = int(item.get("photo_count") or 0)

        weekday_label = self.t(f"weekday.{weekday_index}") if 0 <= weekday_index <= 6 else ""
        time_of_day_label = self.t(f"time_of_day.{time_of_day_key}") if time_of_day_key else ""

        title_bits = [date]
        if weekday_label:
            title_bits.append(weekday_label)
        if time_of_day_label:
            title_bits.append(time_of_day_label)
        if location:
            title_bits.append(location)
        if device:
            title_bits.append(device)
        title = " \u00b7 ".join(title_bits)

        place_label = location or self.t("summary.place_unknown")
        photo_count_label = self.t(
            "summary.photo_count_one" if photo_count == 1 else "summary.photo_count_many",
            count=photo_count,
        )
        when_label = self.t(
            "summary.when",
            weekday=weekday_label,
            time_of_day=time_of_day_label,
            date=date,
        ).strip()
        summary_key = "summary.session_with_device" if device else "summary.session_no_device"
        summary = self.t(
            summary_key,
            when=when_label,
            count=photo_count,
            photo_count=photo_count_label,
            place=place_label,
            device=device,
        ).strip()

        first_ts = float(item.get("first_capture_ts") or 0.0)
        last_ts = float(item.get("last_capture_ts") or 0.0)
        if first_ts and last_ts and last_ts > first_ts:
            try:
                start_str = _time.strftime("%H:%M", _time.localtime(first_ts))
                end_str = _time.strftime("%H:%M", _time.localtime(last_ts))
                summary = self.t(
                    "summary.with_time_range",
                    summary=summary,
                    start=start_str,
                    end=end_str,
                )
            except (OSError, OverflowError, ValueError):
                pass

        # Hero thumbnails: first / middle / last representative.
        reps: list[dict[str, Any]] = list(item.get("representative_photos") or [])
        content_blocks: list[ContentBlock] = []
        hero_indices = (
            [0, len(reps) // 2, len(reps) - 1] if len(reps) >= 3 else list(range(len(reps)))
        )
        seen_paths: set[str] = set()
        for idx in hero_indices:
            path = str(reps[idx].get("path", "")) if 0 <= idx < len(reps) else ""
            if path and path not in seen_paths:
                content_blocks.append(ContentBlock(kind="image", value=path))
                seen_paths.add(path)

        provenance: dict[str, Any] = {
            "source_id": self.source_id,
            "session_key": str(item.get("session_key") or ""),
            "date": date,
            "weekday_index": weekday_index if 0 <= weekday_index <= 6 else None,
            "time_of_day": time_of_day_key,
            "device_name": device,
            "device_slug": str(item.get("device_slug") or ""),
            "location_name": location,
            "location_source": str(item.get("location_source") or ""),
            "apple_photos_place_name": str(item.get("apple_photos_place_name") or ""),
            "apple_photos_place_address": str(item.get("apple_photos_place_address") or ""),
            "latitude": item.get("latitude"),
            "longitude": item.get("longitude"),
            "photo_count": photo_count,
            "burst_total": int(item.get("burst_total") or 0),
            "first_capture_ts": first_ts,
            "last_capture_ts": last_ts,
        }

        tags = build_session_retrieval_terms(item)

        domain_payload = {
            "representative_photos": reps,
            "source_facets": build_session_source_facets(item),
        }

        return self._build_output(
            source_item_id=str(item.get("session_key") or ""),
            activity=self._build_activity(
                source=self._build_activity_facet(
                    code="photos",
                    i18n_key="activity.source.photos",
                    fallback="Photos",
                    embedding_fallback="照片",
                ),
                action=self._build_activity_facet(
                    code="capture",
                    i18n_key="activity.action.capture",
                    fallback="Capture",
                    embedding_fallback="拍摄",
                ),
                object=self._build_activity_facet(
                    code="photo",
                    i18n_key="activity.object.photo",
                    fallback="Photo",
                    embedding_fallback="照片",
                ),
                qualifiers={"session_type": "photo_session"},
            ),
            narration=self._build_narration(title=title, body=summary),
            occurred_at=first_ts,
            raw_payload_ref=None,
            content_blocks=content_blocks,
            tags=tags,
            provenance=provenance,
            domain_payload=domain_payload,
        )

    async def extract_metadata(self, item: dict[str, Any]) -> SourceOutputMetadata:
        tags = build_session_retrieval_terms(item)
        return SourceOutputMetadata(
            entities=build_session_entity_hints(item),
            tags=tags,
            fact_hints=build_session_fact_hints(item),
            relation_candidates=build_session_relation_candidates(item),
        )
