"""Screenshot timeline sensor — orchestrates triggers, helper, privacy, perceptual-hash dedup, and per-capture L1 emission.

Storage model: **one screenshot = one L1 event**.

Rationale: an earlier version of this sensor aggregated consecutive
same-window captures into "bursts" and emitted one L1 row per burst.
That made each row carry 5–8 KB of OCR text union, which the host
embedder split into 12–20 chunks per row. In multi-source retrieval
(e.g. side-by-side with chrome_history's short rows) a single burst
dominated top-K via sheer chunk count, drowning out other sources.

Now we emit per-capture rows (≈1–2 KB OCR each, 1–2 chunks). Burst is
still a useful UI concept (group consecutive same-window captures on a
timeline) but it lives in the UI/projection layer, not in storage.

To stop bombing L1 with near-identical frames (cursor blink, idle
window) we use the Swift helper's dHash output and drop captures whose
hash is within hamming distance 5 of the previous same-window capture.
"""
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
    from .phash_utils import hamming_distance
    from .session_tracker import (
        DEFAULT_IDLE_THRESHOLD_SECONDS,
        SessionStore,
        SessionTracker,
    )
    from .helper_client import HelperClient, HelperCrashedError, HelperTimeoutError
    from .ids import new_capture_id
    from .permissions import request_screen_recording, screen_recording_status
    from .privacy_guard import PrivacyGuard
    from .retention import purge_orphan_originals
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

    _ph = _load_sibling("phash_utils")
    hamming_distance = _ph.hamming_distance
    _st = _load_sibling("session_tracker")
    DEFAULT_IDLE_THRESHOLD_SECONDS = _st.DEFAULT_IDLE_THRESHOLD_SECONDS
    SessionStore = _st.SessionStore
    SessionTracker = _st.SessionTracker
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
    _to = _load_sibling("trigger_orchestrator")
    IntervalTimer = _to.IntervalTimer
    TriggerOrchestrator = _to.TriggerOrchestrator
    install_nsworkspace_observer = _to.install_nsworkspace_observer

logger = logging.getLogger(__name__)


class ScreenshotSensor(SensorBase):
    """Pull-sync sensor that emits one L1 event per screenshot capture."""

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
        retention_days: int = 30,
        capture_scope: str = "hybrid",
        ocr_languages: tuple[str, ...] = ("en-US", "zh-Hans"),
        ocr_level: str = "accurate",
        ax_enabled: bool = True,
        ax_wake: bool = True,
        ax_min_content_chars: int = 80,
        ax_min_content_nodes: int = 5,
        extra_app_blocklist: tuple[str, ...] = (),
        window_title_blocklist: tuple[str, ...] = (),
        thumbnail_max_width: int = 1024,
        jpeg_quality_original: int = 80,
        jpeg_quality_thumbnail: int = 70,
        active_window_interval_sec: float = 10.0,
        full_screen_interval_min: float = 5.0,
        phash_dedup_threshold: int = 5,
        session_idle_threshold_seconds: float | None = None,
        session_db_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.helper_argv = list(helper_argv or [])
        self.resources_root = (
            Path(resources_root)
            if resources_root is not None
            else Path.home() / ".magi" / "data" / "resources" / "screenshots"
        )
        self.retention_days = retention_days
        self.capture_scope = capture_scope
        self.ocr_languages = tuple(ocr_languages)
        self.ocr_level = ocr_level
        # AX-first content extraction. When the focused window's accessibility
        # tree carries enough in-content text (>= both thresholds), the helper
        # uses it and skips OCR; hollow trees (WeChat/QQ/games) fall back to
        # OCR. Thresholds are deliberately low — the hollow-vs-rich gap is ~100x.
        self.ax_enabled = bool(ax_enabled)
        self.ax_wake = bool(ax_wake)
        self.ax_min_content_chars = int(ax_min_content_chars)
        self.ax_min_content_nodes = int(ax_min_content_nodes)
        self.thumbnail_max_width = thumbnail_max_width
        self.jpeg_quality_original = jpeg_quality_original
        self.jpeg_quality_thumbnail = jpeg_quality_thumbnail
        self.active_window_interval_sec = float(active_window_interval_sec)
        self.full_screen_interval_min = float(full_screen_interval_min)
        # Hamming distance threshold below which a new capture is treated
        # as a redundant near-duplicate of the previous one for the same
        # window. ~5 out of 64 bits ≈ cursor / minor anti-alias differences.
        self.phash_dedup_threshold = int(phash_dedup_threshold)

        self._helper: HelperClient | None = (
            HelperClient(binary_argv=self.helper_argv) if self.helper_argv else None
        )
        self._guard = PrivacyGuard(
            extra_app_blocklist=tuple(extra_app_blocklist),
            window_title_blocklist=tuple(window_title_blocklist),
        )
        # Per-window last phash (hex) — keyed by (app_bundle, window_title).
        # Used to suppress near-duplicate frames. Bounded by app/window
        # cardinality, which is small in practice.
        self._last_phash_by_window: dict[tuple[str, str], str] = {}
        # Per-capture L1 items ready for the next collect_items() pull.
        self._pending_items: list[dict[str, Any]] = []

        # Plugin-private session tracker. Stays in a SQLite db under
        # ~/.magi/data/plugins/screenshot_timeline/ — NOT in host memory.
        # We open it lazily in start() so tests can override the path
        # via __init__ before any disk I/O happens.
        self._session_db_path = (
            Path(session_db_path)
            if session_db_path is not None
            else Path.home() / ".magi" / "data" / "plugins" / "screenshot_timeline" / "sessions.db"
        )
        self.session_idle_threshold_seconds = (
            float(session_idle_threshold_seconds)
            if session_idle_threshold_seconds is not None
            else DEFAULT_IDLE_THRESHOLD_SECONDS
        )
        self._session_store: SessionStore | None = None
        self._session_tracker: SessionTracker | None = None

        # Wired up by start() (lazy, triggered on first collect_items()),
        # torn down by stop() if it's ever invoked.
        self._orchestrator: TriggerOrchestrator | None = None
        self._active_timer: IntervalTimer | None = None
        self._full_screen_timer: IntervalTimer | None = None
        self._workspace_handle: object | None = None
        self._retention_task: asyncio.Task | None = None
        self._started: bool = False
        self._start_lock: asyncio.Lock = asyncio.Lock()

    # ------- Lifecycle -------

    async def start(self) -> None:
        """Idempotent lazy init.

        SensorBase has no host-driven start/stop hook (the host only calls
        collect_items periodically). So we self-bootstrap on the first
        collect_items() call, and this method is a no-op on subsequent
        invocations. Also safe to call from tests directly.
        """
        # Fast path without lock to avoid contention.
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            await self._do_start()
            self._started = True

    async def _do_start(self) -> None:
        logger.info(
            "sensor.start.begin helper_argv=%s capture_scope=%s active_interval_s=%s full_screen_interval_min=%s",
            self.helper_argv, self.capture_scope, self.active_window_interval_sec,
            self.full_screen_interval_min,
        )
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
            logger.warning("sensor.permission_probe_failed", exc_info=True)
            status = "unknown"
        logger.info("sensor.start.permission_status=%s", status)
        if status == "denied":
            logger.warning(
                "sensor.permission_blocked status=%s — skipping helper start. "
                "User must grant Screen Recording in System Settings → Privacy & Security.",
                status,
            )
            return

        if self._helper is not None:
            await self._helper.start()
            logger.info("sensor.start.helper_spawned argv=%s", self.helper_argv)

        # Plugin-private session db / tracker. Recovery of any stale
        # 'open' session row happens inside SessionTracker.__init__.
        self._session_store = SessionStore(self._session_db_path)
        self._session_tracker = SessionTracker(
            store=self._session_store,
            idle_threshold_seconds=self.session_idle_threshold_seconds,
        )
        logger.info(
            "sensor.start.session_tracker_ready db=%s idle_threshold_s=%.0f",
            self._session_db_path, self.session_idle_threshold_seconds,
        )

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
        logger.info(
            "sensor.start.timers_installed active=%ss full_screen_timer=%s observer=%s",
            self.active_window_interval_sec,
            self._full_screen_timer is not None,
            self._workspace_handle is not None,
        )

        self._retention_task = asyncio.create_task(self._retention_loop())
        logger.info("sensor.start.complete — capture loop now live")

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

        # Drain pending L1 items before tearing down the helper subprocess
        # so that anything in flight from a recent tick won't be lost.
        await self.drain_pending_items()
        if self._helper is not None:
            await self._helper.shutdown()
        # Close any open session record cleanly + release the db handle.
        if self._session_tracker is not None:
            try:
                self._session_tracker.shutdown()
            except Exception:  # noqa: BLE001
                logger.exception("session.shutdown_failed")
            self._session_tracker = None
        if self._session_store is not None:
            self._session_store.close()
            self._session_store = None

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
        """Hand off all captures queued since the last poll.

        Also handles lazy start: SensorBase has no host-driven start hook,
        so the very first call to collect_items() is what actually kicks
        off the helper subprocess + timer loops + window-switch observer.

        Each item corresponds to exactly one screenshot — see the module
        docstring for why we no longer aggregate bursts at storage time.
        """
        logger.debug(
            "sensor.collect_items.called started=%s pending=%d",
            self._started, len(self._pending_items),
        )
        if not self._started:
            try:
                await self.start()
            except Exception as exc:  # noqa: BLE001
                logger.exception("sensor.lazy_start_failed err=%r", exc)
                return SensorSyncResult(items=[])
        items = list(self._pending_items)
        self._pending_items.clear()
        return SensorSyncResult(items=items)

    async def build_output(self, item: dict[str, Any]) -> SensorOutput:
        """Render one captured screenshot into a SensorOutput.

        ``item`` is the per-image payload assembled in ``trigger_once()``.
        The capture's id is its L1 source_item_id, so each capture becomes
        exactly one host-side L1 row.
        """
        app_bundle = str(item.get("app_bundle") or "")
        app_name = str(item.get("app_name") or app_bundle)
        window_title = str(item.get("window_title") or "")
        display_id = str(item.get("display_id") or "primary")
        trigger = str(item.get("trigger") or "")
        scope = str(item.get("scope") or "")

        activity = self._build_activity(
            source=self._build_activity_facet(
                code="screenshot_timeline",
                i18n_key="activity.source.screenshot_timeline",
                fallback="Screenshot Timeline",
            ),
            action=self._build_activity_facet(
                code="screen_capture",
                i18n_key="activity.action.screen_capture",
                fallback="Screen Capture",
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
                "trigger": trigger,
                "scope": scope,
            },
        )

        title = (
            f"{app_name}: {window_title}".strip(": ").strip() if window_title else app_name
        )
        ocr_text = str(item.get("ocr_text") or "")
        ax_text = str(item.get("ax_text") or "")
        used_ocr = bool(item.get("used_ocr_fallback", True))
        # AX-first: when the helper found a rich accessibility tree, surface its
        # exact text; otherwise (hollow tree / OCR fallback) surface OCR. Guard
        # both directions so an empty primary still yields the other.
        body = (ocr_text if used_ocr else ax_text) or ax_text or ocr_text
        narration = self._build_narration(title=title or None, body=body)
        content_blocks = [ContentBlock(kind="text", value=body)]

        tags: list[str] = []
        if app_bundle:
            tags.append(f"app:{app_bundle}")
        tags.append(f"display:{display_id}")

        provenance = {
            "sensor_id": self.sensor_id,
            "capture_id": str(item.get("capture_id") or ""),
            "trigger": trigger,
            "scope": scope,
            "ocr_confidence_avg": float(item.get("ocr_confidence_avg") or 0.0),
            # Which extractor produced the content block, plus the AX score that
            # drove the decision. The full ax_blocks aren't persisted here (a
            # rich window is 100s of nodes — too heavy per row); they stay on
            # the in-flight item for any future entity-extraction pass.
            "content_source": "ocr" if used_ocr else "ax",
            "ax_content_chars": int(item.get("ax_content_chars") or 0),
            "ax_node_count": int(item.get("ax_node_count") or 0),
            "phash": str(item.get("phash") or ""),
            "original_path": str(item.get("original_path") or ""),
            "thumbnail_path": str(item.get("thumbnail_path") or ""),
            "original_expires_at": item.get("original_expires_at"),
            "dimensions": list(item.get("dimensions") or [0, 0]),
            # Plugin-private session id this capture belongs to. The
            # session row itself lives in the plugin's sessions.db; we
            # write only the id here so a later host-side query can
            # group L1 events by session if needed.
            "session_id": str(item.get("session_id") or ""),
        }

        return self._build_output(
            source_item_id=str(item["source_item_id"]),
            activity=activity,
            narration=narration,
            occurred_at=float(item.get("captured_at") or 0.0),
            content_blocks=content_blocks,
            tags=tags,
            provenance=provenance,
            domain_payload={
                "importance_score": _importance_for_capture(item),
            },
        )

    async def extract_metadata(self, item: dict[str, Any]) -> SensorOutputMetadata:
        entities: list[dict[str, Any]] = []
        app_bundle = str(item.get("app_bundle") or "")
        app_name = str(item.get("app_name") or app_bundle)
        if app_bundle:
            entities.append({"type": "software", "name": app_name, "canonical_id": app_bundle})
        # Intentionally do NOT emit window_title as a topic entity. On most
        # apps the window title is chrome (file path tabs in editors, mail
        # subject lines, browser tab titles after the page name) — not a
        # canonical topic. Forwarding it to the KG floods it with strings
        # like "cap_01KS614703Y9D2H3482FW77TC4_thumb.jpg" that have no
        # semantic value. Real topic extraction belongs at the host level
        # downstream of the OCR text, not here.
        url_value = str(item.get("url") or "").strip()
        if url_value:
            # Two entities per URL — keeps KG joinable across plugins:
            #   web_page: the exact URL, useful for "I saw this specific page"
            #   site:    just the host, matches chrome-history's canonical
            #            `site:<domain>` node id so visits in one plugin
            #            line up with on-screen visits in this one.
            entities.append(
                {"type": "web_page", "name": url_value, "canonical_id": url_value}
            )
            domain = _domain_from_url(url_value)
            if domain:
                entities.append(
                    {"type": "site", "name": domain, "canonical_id": f"site:{domain}"}
                )
        return SensorOutputMetadata(entities=entities)

    # ------- Live capture path -------

    async def trigger_once(self, trigger: str) -> None:
        """Run a single capture cycle. Called by the trigger orchestrator."""
        logger.debug("trigger.fire trigger=%s", trigger)
        if self._helper is None:
            logger.warning("trigger.no_helper — sensor wasn't fully started; skipping")
            return
        rid = new_capture_id()

        # 1. Probe active window
        try:
            probe = await self._helper.request({"id": f"{rid}_probe", "op": "probe_active_window"})
        except (HelperCrashedError, HelperTimeoutError) as exc:
            logger.warning("trigger.probe_failed trigger=%s err=%r", trigger, exc)
            return
        if not probe.get("ok"):
            logger.warning("trigger.probe_not_ok trigger=%s resp=%s", trigger, probe)
            return
        win = probe.get("active_window") or {}
        logger.debug(
            "trigger.probed trigger=%s app=%s window=%r",
            trigger, win.get("app_bundle_id"), win.get("window_title"),
        )

        # 2. Privacy filter
        screen_locked = await self._probe_screen_lock()
        skip_reason = self._guard.should_skip_capture(
            app_bundle=str(win.get("app_bundle_id") or ""),
            window_title=str(win.get("window_title") or ""),
            screen_locked=screen_locked,
            now=time.time(),
        )
        if skip_reason is not None:
            logger.info(
                "trigger.skipped reason=%s trigger=%s app=%s locked=%s",
                skip_reason, trigger, win.get("app_bundle_id"), screen_locked,
            )
            return

        # 3. Compose file paths
        #
        # Layout: split originals and thumbnails into sibling subtrees with
        # the same filename in each. This lets retention sweep originals
        # without ever touching thumbnails, and lets downstream readers
        # (chat / memory / timeline UI) store just the capture_id and
        # resolve to either side by prepending the right prefix.
        #
        #   <resources_root>/originals/<YYYY/MM/DD>/<capture_id>.jpg
        #   <resources_root>/thumbnails/<YYYY/MM/DD>/<capture_id>.jpg
        now = time.time()
        date_subpath = time.strftime("%Y/%m/%d", time.localtime(now))
        original_path = str(self.resources_root / "originals" / date_subpath / f"{rid}.jpg")
        thumbnail_path = str(self.resources_root / "thumbnails" / date_subpath / f"{rid}.jpg")
        logger.debug("trigger.capture_request rid=%s path=%s", rid, original_path)

        # 4. Capture + OCR via helper
        try:
            resp = await self._helper.request(
                {
                    "id": rid,
                    "op": "capture_and_ocr",
                    "scope": _scope_for_trigger(self.capture_scope, trigger),
                    "ocr": {"languages": list(self.ocr_languages), "level": self.ocr_level},
                    "ax": {
                        "enabled": self.ax_enabled,
                        "wake": self.ax_wake,
                        "min_content_chars": self.ax_min_content_chars,
                        "min_content_nodes": self.ax_min_content_nodes,
                    },
                    "save_paths": {"original": original_path, "thumbnail": thumbnail_path},
                    "jpeg_quality": {
                        "original": self.jpeg_quality_original,
                        "thumbnail": self.jpeg_quality_thumbnail,
                    },
                    "thumbnail_max_width": self.thumbnail_max_width,
                }
            )
        except (HelperCrashedError, HelperTimeoutError) as exc:
            logger.warning("trigger.capture_failed trigger=%s err=%r", trigger, exc)
            return

        if not resp.get("ok"):
            logger.warning("trigger.helper_error %s", resp.get("error"))
            return

        logger.debug(
            "trigger.captured rid=%s dims=%s ocr_chars=%d files=%s",
            rid, resp.get("dimensions"),
            len((resp.get("ocr") or {}).get("text") or ""),
            resp.get("files_written"),
        )

        # 5. Perceptual-hash dedup against the previous capture of the same window.
        #    The helper has already written the jpgs to disk; if we decide
        #    to drop this capture we also delete those files so nothing
        #    leaks into retention's pool.
        app_bundle = str(win.get("app_bundle_id") or "")
        window_title = str(win.get("window_title") or "")
        window_key = (app_bundle, window_title)
        phash = str(resp.get("phash") or "")
        if phash and window_key in self._last_phash_by_window:
            dist = hamming_distance(self._last_phash_by_window[window_key], phash)
            if dist <= self.phash_dedup_threshold:
                logger.info(
                    "trigger.skipped reason=phash_dup trigger=%s app=%s window=%r dist=%d",
                    trigger, app_bundle, window_title, dist,
                )
                # Best-effort cleanup of the freshly-written jpgs.
                for p in (original_path, thumbnail_path):
                    try:
                        Path(p).unlink(missing_ok=True)
                    except OSError:
                        pass
                # Still record the phash so a steady-state near-identical
                # stream remains suppressed (don't overwrite with the
                # baseline — the new frame is similar enough to be a no-op).
                return
        if phash:
            self._last_phash_by_window[window_key] = phash

        # 6. Update the session tracker. May close the previous session
        # (returned as `closed`) and/or open a new one. We tag the item
        # below with whichever session id is current after observe.
        captured_at = float(resp.get("captured_at") or now)
        idle_seconds_raw = resp.get("idle_seconds")
        idle_seconds = (
            float(idle_seconds_raw) if isinstance(idle_seconds_raw, (int, float)) else None
        )
        session_id: str | None = None
        if self._session_tracker is not None:
            try:
                _closed, current = self._session_tracker.observe_capture(
                    capture_id=rid,
                    captured_at=captured_at,
                    app_bundle=app_bundle,
                    idle_seconds=idle_seconds,
                    screen_locked=screen_locked,
                )
                session_id = current.session_id
            except Exception:  # noqa: BLE001
                # Session tracking is enrichment; if the db is corrupt or
                # the disk is full, capture should still flow into L1.
                logger.exception("session.observe_failed")

        # 7. Build per-capture L1 item and queue it for the next pull.
        item: dict[str, Any] = {
            "capture_id": rid,
            "source_item_id": rid,
            "idempotency_key": rid,
            "captured_at": captured_at,
            "app_bundle": app_bundle,
            "app_name": str(win.get("app_name") or _app_name_from_bundle(app_bundle)),
            "window_title": window_title,
            "url": win.get("url"),
            "display_id": str(win.get("display_id") or "primary"),
            "ocr_text": (resp.get("ocr") or {}).get("text") or "",
            "ocr_confidence_avg": float((resp.get("ocr") or {}).get("confidence_avg") or 0.0),
            # AX-first content path. `used_ocr_fallback` tells build_output which
            # text to surface; defaults True so a helper that didn't send AX
            # fields degrades to the original OCR behavior.
            "ax_text": str(resp.get("ax_text") or ""),
            "ax_content_chars": int(resp.get("ax_content_chars") or 0),
            "ax_node_count": int(resp.get("ax_node_count") or 0),
            "ax_blocks": resp.get("ax_blocks") or [],
            "used_ocr_fallback": bool(resp.get("used_ocr_fallback", True)),
            "trigger": trigger,
            "scope": _scope_for_trigger(self.capture_scope, trigger),
            "original_path": original_path,
            "thumbnail_path": thumbnail_path,
            "original_expires_at": captured_at + self.retention_days * 86400.0,
            "dimensions": list(resp.get("dimensions") or [0, 0]),
            "phash": phash,
            "idle_seconds": idle_seconds,
            "session_id": session_id,
        }
        self._pending_items.append(item)

    async def _probe_screen_lock(self) -> bool:
        """Probe whether the macOS screen is locked, via the long-running helper.

        Reuses the existing HelperClient subprocess (held in ``self._helper``) so
        every capture cycle doesn't pay the ~50ms cost of spawning a fresh
        helper. Returns False on any failure (no helper, crash, timeout, ok=False
        response) so capture is not falsely suppressed.
        """
        if self._helper is None:
            return False
        try:
            resp = await self._helper.request({
                "id": f"lock_{int(time.time() * 1000)}",
                "op": "probe_screen_lock",
            })
        except (HelperCrashedError, HelperTimeoutError):
            return False
        if not resp.get("ok"):
            return False
        return bool(resp.get("screen_locked", False))

    async def drain_pending_items(self) -> list[dict[str, Any]]:
        """Return + clear all queued per-capture items.

        Used at shutdown to make sure no in-flight item is lost, and in
        tests to assert on what was captured without going through the
        host pull-sync path.
        """
        out = list(self._pending_items)
        self._pending_items.clear()
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


def _scope_for_trigger(default_scope: str, trigger: str) -> str:
    # The dedicated full-screen timer emits "full_screen_timer" → always full_screen.
    if trigger == "full_screen_timer":
        return "full_screen"
    # In hybrid mode every other trigger uses active_window.
    if default_scope == "hybrid":
        return "active_window"
    return default_scope


def _importance_for_capture(item: dict[str, Any]) -> float:
    """Per-capture importance — single-trigger version of the prior
    burst-weighted formula. Captures driven by an explicit user
    intention (manual, window switch) outrank passive timer ticks."""
    trigger = str(item.get("trigger") or "")
    if trigger == "manual":
        return 0.8
    if trigger == "window_switch":
        return 0.5
    return 0.3


def _domain_from_url(url: str) -> str:
    """Return the bare host of an HTTP(S) URL, lowercased, with leading
    ``www.`` stripped. Returns empty string on anything that doesn't
    parse — we never want to invent a domain.

    Matches chrome-history's normalize_domain() in spirit so the L2
    ``site:<domain>`` node id is the same across the two plugins.
    """
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(url.strip())
    except Exception:  # noqa: BLE001
        return ""
    if parts.scheme not in ("http", "https"):
        return ""
    host = (parts.hostname or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _app_name_from_bundle(bundle: str) -> str:
    """Derive a display name from an app bundle id when we don't have one.

    The helper already returns ``app_name`` separately so this is mostly
    a safety net (probe returned a bundle but no localized name). Pure
    fallback heuristic — strip the reverse-DNS prefix and titlecase the
    last component (``com.apple.Safari`` → ``Safari``).
    """
    if not bundle:
        return ""
    last = bundle.rsplit(".", 1)[-1]
    return last or bundle


__all__ = ["ScreenshotSensor"]
