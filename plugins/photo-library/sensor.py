"""Timeline sensor for local photo libraries.

Emits one L1 event per *photo session* 鈥?a coherent shooting activity
defined by (local date, device, geo cell) with cross-midnight merging.
Per-photo records are not surfaced individually: a typical user shoots
hundreds of photos that compress into a handful of memorable sessions,
and that is the unit the memory layer should index.
"""
from __future__ import annotations

import asyncio
import hashlib
import time as _time
from pathlib import Path
from typing import Any

from magi_plugin_sdk.sensors import (
    ContentBlock,
    L2BatchPolicy,
    SensorBase,
    SensorMemoryPolicy,
    SensorOutput,
    SensorOutputMetadata,
    SensorSyncContext,
    SensorSyncResult,
)

from .geocoder import batch_lookup as _geo_batch_lookup, format_location
from .file_index import FileIndexCache
from .locale_data import get_locale_map
from .normalizers import (
    build_session_entity_hints,
    build_session_relation_candidates,
)
from .reader import PhotoLibraryReader
from .sessions import aggregate_sessions


# Sessions with no new captures within this window are emitted to L1.
# Younger sessions are deferred so each session is written exactly once
# with its full content (L1 store is INSERT OR IGNORE on idempotency_key).
_DEFAULT_SETTLE_WINDOW_SECONDS = 4 * 3600


class PhotoLibraryTimelineSensor(SensorBase):
    """Pull-sync sensor that aggregates a local photo directory into sessions."""

    sensor_id = "timeline.photo_library"
    display_name = "Photo Library"
    source_type = "photo_library"
    polling_mode = "interval"
    default_interval = 60
    update_key_fields = ("session_key",)
    relation_edge_whitelist = ("OWNED_DEVICE", "VISITED")
    supports_pull_sync = True

    memory_policy = SensorMemoryPolicy(
        retention_class="compressible",
        cognition_eligible=True,
        importance_bias=0.6,
    )

    def __init__(
        self,
        *,
        source_paths: list[str] | None = None,
        max_items_per_sync: int = 200,
        analysis_features: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        settle_window_seconds: float = _DEFAULT_SETTLE_WINDOW_SECONDS,
        reader: PhotoLibraryReader | None = None,
    ) -> None:
        super().__init__()
        self.source_paths = source_paths or []
        self.max_items_per_sync = max_items_per_sync
        self.analysis_features = analysis_features or ["exif"]
        self.exclude_patterns = exclude_patterns or []
        self.settle_window_seconds = settle_window_seconds
        self._reader = reader or PhotoLibraryReader()

    # ------------------------------------------------------------------
    # Identity & dedup
    # ------------------------------------------------------------------

    def source_item_identity(self, item: dict[str, Any]) -> str:
        return str(item.get("session_key") or "session:unknown")

    def source_item_version_fingerprint(self, item: dict[str, Any]) -> str:
        # Sessions are emitted exactly once when settled, so the fingerprint
        # only needs to capture identity (not content version).
        return hashlib.sha1(
            self.source_item_identity(item).encode("utf-8")
        ).hexdigest()

    # ------------------------------------------------------------------
    # L2 batching
    # ------------------------------------------------------------------

    def l2_batch_policy(self, output: SensorOutput) -> L2BatchPolicy | None:
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

    async def collect_items(self, context: SensorSyncContext) -> SensorSyncResult:
        sensor_settings = (
            context.plugin_settings.get("sensors", {}).get(self.source_type, {})
            if isinstance(context.plugin_settings.get("sensors", {}), dict)
            else {}
        )

        # Resolve source paths: prefer settings list, fall back to instance.
        source_paths: list[str] = []
        raw_paths = sensor_settings.get("source_paths")
        if isinstance(raw_paths, list):
            source_paths = [str(p) for p in raw_paths if p]
        if not source_paths:
            source_paths = list(self.source_paths)

        if not source_paths:
            return SensorSyncResult(
                items=[],
                stats={"count": 0, "error": "source_paths not configured"},
            )

        raw_excludes = sensor_settings.get("exclude_patterns")
        exclude_patterns = (
            [str(p) for p in raw_excludes if p]
            if isinstance(raw_excludes, list)
            else list(self.exclude_patterns)
        )

        # The cursor stores the max modified_at of photos that have already
        # contributed to a *settled* (emitted) session. We keep a look-back
        # window so unfinalized recent sessions get re-evaluated each sync.
        now_ts = _time.time()
        last_cursor = 0.0
        if context.last_cursor:
            try:
                last_cursor = float(context.last_cursor)
            except (ValueError, TypeError):
                last_cursor = 0.0
        look_back = now_ts - self.settle_window_seconds * 2
        min_modified_at = max(0.0, min(last_cursor, look_back))

        # The reader's per-scan limit caps photos, not sessions.
        photo_limit = max(self.max_items_per_sync, 1000)

        analysis_features = list(
            sensor_settings.get("analysis_features", self.analysis_features)
        )

        file_index: FileIndexCache | None = None
        if "exif" in analysis_features:
            try:
                cache_dir = context.runtime_paths.plugin_cache_dir("photo-library")
                file_index = FileIndexCache(cache_dir)
            except Exception:
                file_index = None
        if file_index is not None:
            self._reader._file_index = file_index

        all_photos: list[dict[str, Any]] = []
        total_scanned = 0
        total_errors = 0

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

            allowed_root = Path(src).expanduser().resolve()
            for item in result.items:
                item_path = Path(str(item.get("path", ""))).resolve()
                if allowed_root in {item_path, *item_path.parents}:
                    all_photos.append(item)

        # Reverse-geocode before session aggregation so location_name
        # participates in the session's representative pick.
        if "geocode" in analysis_features and all_photos:
            cache_dir = context.runtime_paths.plugin_cache_dir("photo-library")
            locale_map = get_locale_map(
                str(context.plugin_settings.get("locale", ""))
            )
            coords = [
                (float(p["latitude"]), float(p["longitude"]))
                for p in all_photos
                if p.get("latitude") is not None and p.get("longitude") is not None
            ]
            indices = [
                i for i, p in enumerate(all_photos)
                if p.get("latitude") is not None and p.get("longitude") is not None
            ]
            if coords:
                geo_results = await asyncio.to_thread(_geo_batch_lookup, coords, cache_dir)
                for idx, geo in zip(indices, geo_results):
                    if geo is not None:
                        all_photos[idx]["location_name"] = format_location(
                            geo, locale_map=locale_map
                        )
                        all_photos[idx]["location_country"] = geo.country_code

        sessions, max_settled_mtime = aggregate_sessions(
            all_photos,
            now_ts=now_ts,
            settle_window_seconds=self.settle_window_seconds,
        )

        # Cap emission per sync to keep individual L2 batches manageable.
        if len(sessions) > self.max_items_per_sync:
            sessions = sessions[: self.max_items_per_sync]
            max_settled_mtime = max(
                float(s.get("max_modified_at") or 0.0) for s in sessions
            )

        # Cursor only advances past photos that contributed to an emitted
        # session. Unsettled recent photos stay within the look-back window.
        next_cursor = context.last_cursor
        watermark_ts = context.last_success_at
        if max_settled_mtime > 0:
            next_cursor = str(max_settled_mtime)
            watermark_ts = max_settled_mtime

        return SensorSyncResult(
            items=sessions,
            next_cursor=next_cursor,
            watermark_ts=watermark_ts,
            stats={
                "count": len(sessions),
                "photos_seen": len(all_photos),
                "total_scanned": total_scanned,
                "errors": total_errors,
            },
        )

    # ------------------------------------------------------------------
    # Output building
    # ------------------------------------------------------------------

    async def fetch_item(self, item: dict[str, Any]) -> dict[str, Any]:
        return dict(item)

    async def build_output(self, item: dict[str, Any]) -> SensorOutput:
        device = str(item.get("device_name") or "").strip()
        location = str(item.get("location_name") or "").strip()
        date = str(item.get("date") or "")
        weekday_index = int(item.get("weekday_index") if item.get("weekday_index") is not None else -1)
        time_of_day_key = str(item.get("time_of_day") or "")
        photo_count = int(item.get("photo_count") or 0)

        weekday_label = (
            self.t(f"weekday.{weekday_index}") if 0 <= weekday_index <= 6 else ""
        )
        time_of_day_label = (
            self.t(f"time_of_day.{time_of_day_key}") if time_of_day_key else ""
        )

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
            "sensor_id": self.sensor_id,
            "session_key": str(item.get("session_key") or ""),
            "date": date,
            "weekday_index": weekday_index if 0 <= weekday_index <= 6 else None,
            "time_of_day": time_of_day_key,
            "device_name": device,
            "device_slug": str(item.get("device_slug") or ""),
            "location_name": location,
            "latitude": item.get("latitude"),
            "longitude": item.get("longitude"),
            "photo_count": photo_count,
            "burst_total": int(item.get("burst_total") or 0),
            "first_capture_ts": first_ts,
            "last_capture_ts": last_ts,
        }

        tags = ["photo_library", "session"]
        if location or item.get("latitude") is not None:
            tags.append("geo")

        domain_payload = {"representative_photos": reps}

        return self._build_output(
            source_item_id=str(item.get("session_key") or ""),
            title=title,
            summary=summary,
            occurred_at=first_ts,
            raw_payload_ref=None,
            content_blocks=content_blocks,
            tags=tags,
            provenance=provenance,
            domain_payload=domain_payload,
        )

    async def extract_metadata(self, item: dict[str, Any]) -> SensorOutputMetadata:
        tags = ["photo_library", "session"]
        if item.get("latitude") is not None or item.get("location_name"):
            tags.append("geo")
        return SensorOutputMetadata(
            entities=build_session_entity_hints(item),
            tags=tags,
            relation_candidates=build_session_relation_candidates(item),
        )
