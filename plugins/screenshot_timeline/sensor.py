"""Screenshot timeline sensor — orchestrates triggers, helper, privacy, bursts, L1 emission."""
from __future__ import annotations

import asyncio
import logging
import time
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

# Intra-plugin imports — fallback for test loader (spec_from_file_location)
try:
    from .burst_aggregator import BurstAggregator, ClosedBurst
    from .helper_client import HelperClient, HelperCrashedError, HelperTimeoutError
    from .ids import new_capture_id
    from .permissions import request_screen_recording, screen_recording_status
    from .privacy_guard import PrivacyGuard
    from .retention import purge_orphan_originals
    from .screen_lock import is_screen_locked
    from .trigger_orchestrator import (
        IntervalTimer,
        TriggerOrchestrator,
        install_nsworkspace_observer,
    )
except ImportError:  # pragma: no cover - exercised when loaded outside package context
    import importlib.util as _imp_util
    import sys as _sys
    from pathlib import Path as _Path

    def _load_sibling(name: str):
        path = _Path(__file__).resolve().parent / f"{name}.py"
        spec = _imp_util.spec_from_file_location(f"screenshot_timeline_{name}_inner", path)
        assert spec is not None and spec.loader is not None
        mod = _imp_util.module_from_spec(spec)
        _sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    _ba = _load_sibling("burst_aggregator")
    BurstAggregator = _ba.BurstAggregator
    ClosedBurst = _ba.ClosedBurst
    _hc = _load_sibling("helper_client")
    HelperClient = _hc.HelperClient
    HelperCrashedError = _hc.HelperCrashedError
    HelperTimeoutError = _hc.HelperTimeoutError
    _ids = _load_sibling("ids")
    new_capture_id = _ids.new_capture_id
    _perm = _load_sibling("permissions")
    request_screen_recording = _perm.request_screen_recording
    screen_recording_status = _perm.screen_recording_status
    _pg = _load_sibling("privacy_guard")
    PrivacyGuard = _pg.PrivacyGuard
    _ret = _load_sibling("retention")
    purge_orphan_originals = _ret.purge_orphan_originals
    _sl = _load_sibling("screen_lock")
    is_screen_locked = _sl.is_screen_locked
    _to = _load_sibling("trigger_orchestrator")
    IntervalTimer = _to.IntervalTimer
    TriggerOrchestrator = _to.TriggerOrchestrator
    install_nsworkspace_observer = _to.install_nsworkspace_observer

logger = logging.getLogger(__name__)


class ScreenshotSensor(SensorBase):
    """Pull-sync sensor that produces burst-aggregated screenshot events."""

    sensor_id: str = "timeline.screenshot"
    display_name: str = "Screenshot Timeline"
    source_type: str = "screenshot_timeline"
    polling_mode: str = "interval"
    default_interval: int = 60
    supports_pull_sync: bool = True
    update_key_fields: tuple[str, ...] = ("source_item_id",)
    memory_policy: SensorMemoryPolicy = SensorMemoryPolicy()

    def __init__(
        self,
        *,
        helper_argv: list[str] | None = None,
        resources_root: Path | None = None,
        gap_minutes: int = 5,
        max_minutes: int = 30,
        retention_days: int = 30,
        capture_scope: str = "hybrid",
        ocr_languages: tuple[str, ...] = ("en-US", "zh-Hans"),
        ocr_level: str = "accurate",
        extra_app_blocklist: tuple[str, ...] = (),
        window_title_blocklist: tuple[str, ...] = (),
        thumbnail_max_width: int = 1024,
        jpeg_quality_original: int = 80,
        jpeg_quality_thumbnail: int = 70,
        active_window_interval_sec: float = 10.0,
        full_screen_interval_min: float = 5.0,
    ) -> None:
        super().__init__()
        self.helper_argv = list(helper_argv or [])
        self.resources_root = (
            Path(resources_root)
            if resources_root is not None
            else Path.home() / ".magi" / "data" / "resources" / "screenshots"
        )
        self.gap_minutes = gap_minutes
        self.max_minutes = max_minutes
        self.retention_days = retention_days
        self.capture_scope = capture_scope
        self.ocr_languages = tuple(ocr_languages)
        self.ocr_level = ocr_level
        self.thumbnail_max_width = thumbnail_max_width
        self.jpeg_quality_original = jpeg_quality_original
        self.jpeg_quality_thumbnail = jpeg_quality_thumbnail
        self.active_window_interval_sec = float(active_window_interval_sec)
        self.full_screen_interval_min = float(full_screen_interval_min)

        self._helper: HelperClient | None = (
            HelperClient(binary_argv=self.helper_argv) if self.helper_argv else None
        )
        self._aggregator = BurstAggregator(
            gap_minutes=gap_minutes,
            max_minutes=max_minutes,
            retention_days=retention_days,
        )
        self._guard = PrivacyGuard(
            extra_app_blocklist=tuple(extra_app_blocklist),
            window_title_blocklist=tuple(window_title_blocklist),
        )
        self._pending_closed: list[dict[str, Any]] = []

        # Wired up in start(), torn down in stop()
        self._orchestrator: TriggerOrchestrator | None = None
        self._active_timer: IntervalTimer | None = None
        self._full_screen_timer: IntervalTimer | None = None
        self._workspace_handle: object | None = None
        self._retention_task: asyncio.Task | None = None

    # ------- Lifecycle -------

    async def start(self) -> None:
        # Trigger the Screen Recording prompt on first enable (no-op on subsequent enables;
        # macOS only shows the dialog once per binary).
        try:
            request_screen_recording()
        except Exception:
            logger.debug("sensor.permission_request_failed", exc_info=True)

        # Refuse to start the helper / capture loop if Screen Recording is denied.
        # macOS caches the user's "no" and will not re-prompt, so attempting to run
        # the helper would just produce a stream of PERMISSION_DENIED errors. We treat
        # ``unknown`` (probe failed — e.g. Quartz unavailable in tests / non-mac dev
        # environments) and ``not_determined`` as soft-allow so existing flows and
        # tests continue to work; only an explicit ``denied`` blocks startup.
        try:
            status = screen_recording_status()
        except Exception:  # noqa: BLE001
            logger.debug("sensor.permission_probe_failed", exc_info=True)
            status = "unknown"
        if status == "denied":
            logger.warning(
                "sensor.permission_blocked status=%s — skipping helper start. "
                "User must grant Screen Recording in System Settings → Privacy & Security.",
                status,
            )
            return

        if self._helper is not None:
            await self._helper.start()

        loop = asyncio.get_running_loop()
        self._orchestrator = TriggerOrchestrator(
            on_capture=self.trigger_once,
            global_debounce_seconds=1.5,
        )

        async def _tick(trigger: str) -> None:
            if self._orchestrator is None:
                return
            await self._orchestrator.emit(trigger)

        self._active_timer = IntervalTimer(
            interval_seconds=self.active_window_interval_sec,
            trigger_label="timer",
            on_tick=_tick,
        )
        await self._active_timer.start()

        if self.capture_scope in ("hybrid", "full_screen"):
            self._full_screen_timer = IntervalTimer(
                interval_seconds=self.full_screen_interval_min * 60.0,
                trigger_label="full_screen_timer",
                on_tick=_tick,
            )
            await self._full_screen_timer.start()

        def _on_window_switch() -> None:
            if self._orchestrator is None:
                return
            try:
                asyncio.run_coroutine_threadsafe(
                    self._orchestrator.emit("window_switch"),
                    loop,
                )
            except RuntimeError:
                # Loop closed or not running — swallow; this runs on AppKit thread.
                pass

        self._workspace_handle = install_nsworkspace_observer(_on_window_switch)

        self._retention_task = asyncio.create_task(self._retention_loop())

    async def stop(self) -> None:
        # Cancel retention sweep first so it cannot race with helper shutdown.
        if self._retention_task is not None:
            self._retention_task.cancel()
            try:
                await asyncio.wait_for(self._retention_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._retention_task = None

        # Stop drivers first so no new captures are issued mid-shutdown.
        if self._active_timer is not None:
            await self._active_timer.stop()
            self._active_timer = None
        if self._full_screen_timer is not None:
            await self._full_screen_timer.stop()
            self._full_screen_timer = None
        if self._workspace_handle is not None:
            try:
                from AppKit import NSWorkspace  # type: ignore[import-not-found]

                nc = NSWorkspace.sharedWorkspace().notificationCenter()
                nc.removeObserver_(self._workspace_handle)
            except Exception:  # noqa: BLE001
                # AppKit unavailable or already-released observer — best effort.
                pass
            self._workspace_handle = None
        self._orchestrator = None

        # Drain any open burst before tearing down the helper subprocess
        await self.flush_pending_bursts()
        if self._helper is not None:
            await self._helper.shutdown()

    # ------- SensorBase overrides -------

    def source_item_identity(self, item: dict[str, Any]) -> str:
        return str(item.get("source_item_id") or "")

    def source_item_version_fingerprint(self, item: dict[str, Any]) -> str:
        return str(item.get("source_item_id") or "")

    def l2_batch_policy(self, output: SensorOutput) -> L2BatchPolicy | None:
        return L2BatchPolicy(
            owner=f"{self.source_type}:default",
            catch_up_owner=f"{self.source_type}:catchup",
            max_events=20,
            min_ready_events=4,
            max_wait_seconds=180,
        )

    async def collect_items(self, context: SensorSyncContext) -> SensorSyncResult:
        """Harvest naturally-closed bursts; do NOT force-close the open one."""
        items = list(self._pending_closed)
        self._pending_closed.clear()
        return SensorSyncResult(items=items)

    async def build_output(self, item: dict[str, Any]) -> SensorOutput:
        app_bundle = str(item.get("app_bundle") or "")
        app_name = str(item.get("app_name") or app_bundle)
        window_title = str(item.get("window_title") or "")
        display_id = str(item.get("display_id") or "primary")

        activity = self._build_activity(
            source=self._build_activity_facet(
                code="screenshot_timeline",
                i18n_key="activity.source.screenshot_timeline",
                fallback="Screenshot Timeline",
            ),
            action=self._build_activity_facet(
                code="screen_session",
                i18n_key="activity.action.screen_session",
                fallback="Screen Session",
            ),
            object=self._build_activity_facet(
                code=app_bundle or "unknown",
                i18n_key=(
                    f"activity.object.app.{app_bundle}" if app_bundle else "activity.object.app.unknown"
                ),
                fallback=app_name or "Unknown App",
            ),
            qualifiers={
                "window_title": window_title,
                "url": str(item.get("url") or ""),
                "display_id": display_id,
                "capture_count": str(item.get("capture_count") or 0),
                "duration_seconds": str(int(item.get("duration_seconds") or 0)),
            },
        )

        title = (
            f"{app_name}: {window_title}".strip(": ").strip() if window_title else app_name
        )
        narration = self._build_narration(
            title=title or None,
            body=str(item.get("ocr_text_union") or ""),
        )

        content_blocks = [
            ContentBlock(kind="text", value=str(item.get("ocr_text_union") or "")),
        ]

        tags: list[str] = []
        if app_bundle:
            tags.append(f"app:{app_bundle}")
        tags.append(f"display:{display_id}")

        provenance = {
            "sensor_id": self.sensor_id,
            "trigger_breakdown": dict(item.get("trigger_breakdown") or {}),
            "ocr_confidence_avg": float(item.get("ocr_confidence_avg") or 0.0),
            "representative_capture_id": str(item.get("representative_capture_id") or ""),
            "captures": list(item.get("captures") or []),
        }

        return self._build_output(
            source_item_id=str(item["source_item_id"]),
            activity=activity,
            narration=narration,
            occurred_at=float(item.get("start_unix") or 0.0),
            content_blocks=content_blocks,
            tags=tags,
            provenance=provenance,
            domain_payload={
                "importance_score": _importance_for_burst_dict(item),
            },
        )

    async def extract_metadata(self, item: dict[str, Any]) -> SensorOutputMetadata:
        entities: list[dict[str, Any]] = []
        app_bundle = str(item.get("app_bundle") or "")
        app_name = str(item.get("app_name") or app_bundle)
        window_title = str(item.get("window_title") or "")
        if app_bundle:
            entities.append({"type": "software", "name": app_name, "canonical_id": app_bundle})
        if window_title:
            entities.append({"type": "topic", "name": window_title})
        if item.get("url"):
            entities.append({"type": "uri", "name": str(item["url"])})
        return SensorOutputMetadata(entities=entities)

    # ------- Live capture path -------

    async def trigger_once(self, trigger: str) -> None:
        """Run a single capture cycle. Called by the trigger orchestrator."""
        if self._helper is None:
            return
        rid = new_capture_id()

        # 1. Probe active window
        try:
            probe = await self._helper.request({"id": f"{rid}_probe", "op": "probe_active_window"})
        except (HelperCrashedError, HelperTimeoutError):
            logger.warning("trigger.probe_failed trigger=%s", trigger)
            return
        if not probe.get("ok"):
            return
        win = probe.get("active_window") or {}

        # 2. Privacy filter
        skip_reason = self._guard.should_skip_capture(
            app_bundle=str(win.get("app_bundle_id") or ""),
            window_title=str(win.get("window_title") or ""),
            screen_locked=is_screen_locked(),
            now=time.time(),
        )
        if skip_reason is not None:
            logger.debug("trigger.skipped reason=%s trigger=%s", skip_reason, trigger)
            return

        # 3. Compose file paths
        now = time.time()
        date_dir = self.resources_root / time.strftime("%Y/%m/%d", time.localtime(now))
        original_path = str(date_dir / f"{rid}_orig.jpg")
        thumbnail_path = str(date_dir / f"{rid}_thumb.jpg")

        # 4. Capture + OCR via helper
        try:
            resp = await self._helper.request(
                {
                    "id": rid,
                    "op": "capture_and_ocr",
                    "scope": _scope_for_trigger(self.capture_scope, trigger),
                    "ocr": {"languages": list(self.ocr_languages), "level": self.ocr_level},
                    "save_paths": {"original": original_path, "thumbnail": thumbnail_path},
                    "jpeg_quality": {
                        "original": self.jpeg_quality_original,
                        "thumbnail": self.jpeg_quality_thumbnail,
                    },
                    "thumbnail_max_width": self.thumbnail_max_width,
                }
            )
        except (HelperCrashedError, HelperTimeoutError):
            logger.warning("trigger.capture_failed trigger=%s", trigger)
            return

        if not resp.get("ok"):
            logger.warning("trigger.helper_error %s", resp.get("error"))
            return

        # 5. Build per-capture payload + feed aggregator
        payload = {
            "capture_id": rid,
            "captured_at": float(resp.get("captured_at") or now),
            "app_bundle": win.get("app_bundle_id") or "",
            "window_title": win.get("window_title") or "",
            "url": win.get("url"),
            "ocr_text": (resp.get("ocr") or {}).get("text") or "",
            "trigger": trigger,
            "scope": _scope_for_trigger(self.capture_scope, trigger),
            "original_path": original_path,
            "thumbnail_path": thumbnail_path,
            "dimensions": tuple(resp.get("dimensions") or (0, 0)),
            "ocr_confidence_avg": float((resp.get("ocr") or {}).get("confidence_avg") or 0.0),
        }
        closed = self._aggregator.ingest(payload)
        for burst in closed:
            self._pending_closed.append(_closed_burst_to_dict(burst))

    async def flush_pending_bursts(self) -> list[dict[str, Any]]:
        """Force-close any open burst and return all pending burst dicts."""
        for burst in self._aggregator.flush_all(now=time.time()):
            self._pending_closed.append(_closed_burst_to_dict(burst))
        out = list(self._pending_closed)
        self._pending_closed.clear()
        return out

    async def _retention_loop(self) -> None:
        """Run retention.purge_orphan_originals every 24h until cancelled."""
        interval = 24 * 3600.0
        # First sweep ~30s after startup (snappy onboarding without fighting a
        # fresh enable storm), then daily.
        first_delay = 30.0
        try:
            await asyncio.sleep(first_delay)
            while True:
                try:
                    stats = purge_orphan_originals(
                        self.resources_root,
                        retention_days=self.retention_days,
                    )
                    if stats["deleted"] > 0 or stats["errors"] > 0:
                        logger.info("retention.sweep stats=%s", stats)
                except Exception:  # noqa: BLE001
                    logger.exception("retention.sweep_failed")
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return


def _closed_burst_to_dict(burst: "ClosedBurst") -> dict[str, Any]:
    return {
        "source_item_id": burst.source_item_id,
        "app_bundle": burst.app_bundle,
        "app_name": burst.app_name,
        "window_title": burst.window_title,
        "url": burst.url,
        "start_unix": burst.start_unix,
        "end_unix": burst.end_unix,
        "duration_seconds": burst.duration_seconds,
        "capture_count": burst.capture_count,
        "captures": [
            {
                "capture_id": c.capture_id,
                "captured_at": c.captured_at,
                "trigger": c.trigger,
                "scope": c.scope,
                "thumbnail_path": c.thumbnail_path,
                "original_path": c.original_path,
                "original_expires_at": c.original_expires_at,
                "dimensions": list(c.dimensions),
            }
            for c in burst.captures
        ],
        "ocr_text_union": burst.ocr_text_union,
        "trigger_breakdown": burst.trigger_breakdown,
        "representative_capture_id": burst.representative_capture_id,
        "ocr_confidence_avg": burst.ocr_confidence_avg,
        "display_id": burst.display_id,
    }


def _scope_for_trigger(default_scope: str, trigger: str) -> str:
    # The dedicated full-screen timer emits "full_screen_timer" → always full_screen.
    if trigger == "full_screen_timer":
        return "full_screen"
    # In hybrid mode every other trigger uses active_window.
    if default_scope == "hybrid":
        return "active_window"
    return default_scope


def _importance_for_burst_dict(item: dict[str, Any]) -> float:
    breakdown = item.get("trigger_breakdown") or {}
    if breakdown.get("manual", 0) > 0:
        return 0.8
    if breakdown.get("window_switch", 0) > 0:
        return 0.5
    return 0.3


__all__ = ["ScreenshotSensor"]
