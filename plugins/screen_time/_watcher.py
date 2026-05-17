"""In-plugin foreground app watcher.

Replaces the macOS-only host-side ``frontmost_app_monitor`` by polling the
foreground application from inside the plugin's own asyncio loop. The watcher
is platform-aware: it uses PyObjC on macOS and ``ctypes`` Win32 calls on
Windows. System calls are dispatched through ``asyncio.to_thread`` so a
sluggish OS call cannot stall the backend event loop.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from .apps import PLATFORM_DARWIN, PLATFORM_WIN32, resolve_app, local_override_path
from .state import ScreenTimeStateStore

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 1.0


ForegroundReader = Callable[[], Optional[tuple[str, str]]]


def _default_reader_for_platform() -> Optional[ForegroundReader]:
    if sys.platform == "darwin":
        from ._macos import read_foreground

        return read_foreground
    if sys.platform == "win32":
        from ._windows import read_foreground

        return read_foreground
    return None


def current_platform() -> str:
    """Return the canonical platform key used by the catalog and provenance."""
    if sys.platform == "darwin":
        return PLATFORM_DARWIN
    if sys.platform == "win32":
        return PLATFORM_WIN32
    return sys.platform


class ForegroundAppWatcher:
    """Polls the foreground app and feeds activation events into the state store."""

    def __init__(
        self,
        *,
        runtime_paths: Any,
        state_store: ScreenTimeStateStore,
        reader: ForegroundReader | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._runtime_paths = runtime_paths
        self._state_store = state_store
        self._reader = reader if reader is not None else _default_reader_for_platform()
        self._poll_interval = max(0.05, float(poll_interval_seconds))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or asyncio.sleep
        self._platform = current_platform()
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_canonical_id: str | None = None

    @property
    def is_supported(self) -> bool:
        return self._reader is not None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        """Start the background poll loop. Returns ``True`` when freshly started."""
        if not self.is_supported:
            logger.debug("Foreground app watcher unsupported on platform %s", self._platform)
            return False
        if self.is_running:
            return False
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="screen_time.watcher")
        return True

    async def stop(self) -> None:
        """Signal the loop to exit and await its completion."""
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            logger.debug("Foreground app watcher stopped", exc_info=True)
        finally:
            self._task = None

    async def poll_once(self) -> bool:
        """Run a single poll iteration. Returns ``True`` if an activation was applied."""
        if not self.is_supported:
            return False
        reader = self._reader
        if reader is None:
            return False
        try:
            sample = await asyncio.to_thread(reader)
        except Exception:
            logger.debug("Foreground app reader raised", exc_info=True)
            return False
        if sample is None:
            return False
        raw_bundle_id, raw_app_name = sample
        if not raw_bundle_id:
            return False

        override_path = local_override_path(self._runtime_paths)
        resolved = resolve_app(
            platform=self._platform,
            raw_bundle_id=raw_bundle_id,
            raw_app_name=raw_app_name,
            override_path=override_path,
        )
        await self._state_store.apply_activation(
            runtime_paths=self._runtime_paths,
            occurred_at=self._clock(),
            bundle_id=raw_bundle_id,
            app_name=raw_app_name,
            canonical_id=resolved.canonical_id,
            display_name=resolved.display_name,
            platform=self._platform,
        )
        self._last_canonical_id = resolved.canonical_id
        return True

    async def _run(self) -> None:
        logger.debug("Foreground app watcher started on platform %s", self._platform)
        try:
            while not self._stop_event.is_set():
                try:
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Foreground app watcher poll failed")
                try:
                    await self._sleep(self._poll_interval)
                except asyncio.CancelledError:
                    raise
        except asyncio.CancelledError:
            logger.debug("Foreground app watcher cancelled")
            raise
        finally:
            logger.debug("Foreground app watcher exited")
