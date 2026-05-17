"""Timeline sensor for cross-platform foreground-app usage."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from magi_plugin_sdk.sensors import (
    ContentBlock,
    SensorBase,
    SensorMemoryPolicy,
    SensorOutput,
    SensorSyncContext,
    SensorSyncResult,
)

from ._watcher import ForegroundAppWatcher
from .state import ScreenTimeStateStore


class ScreenTimeTimelineSensor(SensorBase):
    """Flush event-driven foreground-app usage buckets into hourly aggregates."""

    sensor_id = "timeline.screen_time"
    display_name = "App Usage"
    source_type = "screen_time"
    memory_event_type = "APP_USAGE_HOURLY"
    polling_mode = "interval"
    default_interval = 300
    update_key_fields = ("bucket_start", "canonical_id")
    supports_pull_sync = True
    supports_state_flush = True

    memory_policy = SensorMemoryPolicy(
        memory_domain="external_activity",
        ingest_target="l1_only",
        cognition_eligible=False,
        tom_depth="none",
        retention_class="compressible",
        importance_bias=0.3,
        author_type="external",
        content_type="observation",
    )

    def __init__(
        self,
        *,
        state_store: ScreenTimeStateStore | None = None,
        poll_interval_seconds: float | None = None,
    ):
        super().__init__()
        self._state_store = state_store or ScreenTimeStateStore()
        self._poll_interval_seconds = poll_interval_seconds
        self._watcher: ForegroundAppWatcher | None = None

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def source_item_identity(self, item: dict[str, Any]) -> str:
        canonical_id = str(item.get("canonical_id") or item.get("bundle_id") or "")
        return f"app_usage:{item.get('bucket_start', '')}:{canonical_id}"

    def source_item_version_fingerprint(self, item: dict[str, Any]) -> str:
        version_parts = [
            str(item.get("bucket_start", "")),
            str(item.get("canonical_id") or item.get("bundle_id", "")),
            str(item.get("duration_seconds", 0)),
            str(item.get("session_count", 0)),
        ]
        return hashlib.sha1("|".join(version_parts).encode("utf-8")).hexdigest()

    def _ensure_watcher(self, runtime_paths: Any) -> None:
        if self._watcher is None:
            kwargs: dict[str, Any] = {
                "runtime_paths": runtime_paths,
                "state_store": self._state_store,
            }
            if self._poll_interval_seconds is not None:
                kwargs["poll_interval_seconds"] = self._poll_interval_seconds
            self._watcher = ForegroundAppWatcher(**kwargs)
        if self._watcher.is_supported and not self._watcher.is_running:
            self._watcher.start()

    async def collect_items(self, context: SensorSyncContext) -> SensorSyncResult:
        self._ensure_watcher(context.runtime_paths)
        now = self._now()
        items = await self._state_store.flush_completed(runtime_paths=context.runtime_paths, now=now)
        items.sort(
            key=lambda item: (
                item.get("bucket_start", ""),
                item.get("canonical_id") or item.get("bundle_id", ""),
            ),
            reverse=True,
        )
        now_ts = now.timestamp()
        return SensorSyncResult(
            items=items,
            next_cursor=str(now_ts),
            watermark_ts=now_ts,
            stats={"count": len(items)},
        )

    async def flush_runtime_state(self, *, runtime_paths: Any, plugin_settings: dict[str, Any]) -> dict[str, Any]:
        _ = plugin_settings
        if self._watcher is not None:
            await self._watcher.stop()
            self._watcher = None
        return await self._state_store.flush_in_progress(runtime_paths=runtime_paths, now=self._now())

    async def build_output(self, item: dict[str, Any]) -> SensorOutput:
        bucket_start = datetime.fromisoformat(str(item["bucket_start"]))
        bucket_end = datetime.fromisoformat(str(item["bucket_end"]))
        duration_seconds = int(item.get("duration_seconds", 0))
        session_count = int(item.get("session_count", 0))
        bundle_id = str(item.get("bundle_id", ""))
        raw_app_name = str(item.get("app_name", bundle_id or "Unknown App"))
        canonical_id = str(item.get("canonical_id") or bundle_id or "unknown")
        display_name = str(item.get("display_name") or raw_app_name or canonical_id)
        platform = str(item.get("platform", ""))

        duration_minutes = max(1, round(duration_seconds / 60))
        local_start = bucket_start.astimezone()
        local_end = bucket_end.astimezone()
        time_range = f"{local_start.strftime('%H:%M')}-{local_end.strftime('%H:%M')}"
        body = self.t(
            "summary.hourly_bucket",
            time_range=time_range,
            minutes=duration_minutes,
            fallback=f"· {time_range} · {duration_minutes} min",
        )

        return self._build_output(
            source_item_id=self.source_item_identity(item),
            activity=self._build_activity(
                source=self._build_activity_facet(
                    code=canonical_id,
                    i18n_key=f"apps.{canonical_id}",
                    fallback=display_name,
                    embedding_fallback=display_name,
                ),
                action=self._build_activity_facet(
                    code="usage",
                    i18n_key="activity.action.usage",
                    fallback="Usage",
                    embedding_fallback="使用",
                ),
            ),
            narration=self._build_narration(body=body),
            occurred_at=bucket_start.timestamp(),
            content_blocks=[
                ContentBlock(kind="text", value=f"App: {display_name}"),
                ContentBlock(kind="text", value=f"Canonical ID: {canonical_id}"),
                ContentBlock(kind="text", value=f"Raw ID: {bundle_id}"),
                ContentBlock(kind="text", value=f"Platform: {platform}"),
                ContentBlock(kind="text", value=f"Bucket: {bucket_start.isoformat()} to {bucket_end.isoformat()}"),
                ContentBlock(kind="text", value=f"Duration: {duration_seconds} seconds"),
                ContentBlock(kind="text", value=f"Sessions: {session_count}"),
            ],
            tags=["screen_time", "app_usage", "hourly"],
            provenance={
                "sensor_id": self.sensor_id,
                "bucket_start": bucket_start.isoformat(),
                "bucket_end": bucket_end.isoformat(),
                "canonical_id": canonical_id,
                "display_name": display_name,
                "raw_bundle_id": bundle_id,
                "raw_app_name": raw_app_name,
                "platform": platform,
                "bundle_id": bundle_id,
                "app_name": display_name,
                "duration_seconds": duration_seconds,
                "session_count": session_count,
            },
            domain_payload={
                "retention_mode": "analyze_only",
                "bucket_start": bucket_start.isoformat(),
                "bucket_end": bucket_end.isoformat(),
                "canonical_id": canonical_id,
                "display_name": display_name,
                "raw_bundle_id": bundle_id,
                "raw_app_name": raw_app_name,
                "platform": platform,
                "bundle_id": bundle_id,
                "app_name": display_name,
                "duration_seconds": duration_seconds,
                "session_count": session_count,
                "source": "plugin_watcher",
            },
        )
