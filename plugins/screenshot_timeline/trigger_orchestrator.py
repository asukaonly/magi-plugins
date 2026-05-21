"""Trigger orchestration: timers, window-switch observer, debouncing."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class Debouncer:
    min_interval_seconds: float
    _last: float = field(default=float("-inf"), init=False)

    def accept(self, *, now: float) -> bool:
        if now - self._last >= self.min_interval_seconds:
            self._last = now
            return True
        return False


@dataclass
class PerKeyDebouncer:
    min_interval_seconds: float
    _last: dict[str, float] = field(default_factory=dict, init=False)

    def accept(self, key: str, *, now: float) -> bool:
        last = self._last.get(key, float("-inf"))
        if now - last >= self.min_interval_seconds:
            self._last[key] = now
            return True
        return False


@dataclass
class IntervalTimer:
    interval_seconds: float
    trigger_label: str
    on_tick: Callable[[str], Awaitable[None]]
    _task: asyncio.Task | None = field(default=None, init=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                return
            except asyncio.TimeoutError:
                try:
                    await self.on_tick(self.trigger_label)
                except Exception:  # noqa: BLE001
                    logger.exception("timer.on_tick_failed label=%s", self.trigger_label)


@dataclass
class TriggerOrchestrator:
    on_capture: Callable[[str], Awaitable[None]]
    global_debounce_seconds: float = 1.5
    _global_debouncer: Debouncer = field(init=False)

    def __post_init__(self) -> None:
        self._global_debouncer = Debouncer(min_interval_seconds=self.global_debounce_seconds)

    async def emit(self, trigger: str, *, now: float | None = None) -> None:
        when = now if now is not None else time.time()
        if not self._global_debouncer.accept(now=when):
            return
        await self.on_capture(trigger)


__all__ = ["Debouncer", "PerKeyDebouncer", "IntervalTimer", "TriggerOrchestrator"]


_LISTENER_CLASS_NAME = "_MagiScreenshotNSWorkspaceListener"


def _get_listener_class() -> type | None:
    """Lazily define (and cache) the Obj-C listener class.

    The Obj-C runtime is process-global, so a class can be registered exactly
    once per process. We cache via `objc.lookUpClass` so that re-imports of
    this module (e.g. under multiple test loaders) reuse the existing class
    instead of redefining it (which raises `objc.error`).
    """
    try:
        import objc  # type: ignore[import-not-found]
        from Foundation import NSObject
    except Exception:  # noqa: BLE001
        return None

    try:
        return objc.lookUpClass(_LISTENER_CLASS_NAME)
    except Exception:  # noqa: BLE001
        pass  # not yet registered — define below

    class _MagiScreenshotNSWorkspaceListener(NSObject):  # type: ignore[misc]
        def initWithCallback_(self, callback):  # noqa: N802
            self = objc.super(_MagiScreenshotNSWorkspaceListener, self).init()
            if self is None:
                return None
            self._cb = callback
            return self

        def onActivate_(self, _notification: object) -> None:  # noqa: N802
            cb = getattr(self, "_cb", None)
            if cb is None:
                return
            try:
                cb()
            except Exception:  # noqa: BLE001
                logger.exception("nsworkspace.callback_failed")

    return _MagiScreenshotNSWorkspaceListener


def install_nsworkspace_observer(callback: Callable[[], None]) -> object | None:
    """Install a macOS NSWorkspace observer that fires `callback()` when the
    frontmost application changes. Returns an opaque handle to retain, or None
    on platforms where this is not supported.

    The caller MUST keep the returned handle alive for the duration of the
    observation (the autorelease pool releases it otherwise).
    """
    try:
        from AppKit import NSWorkspace
    except Exception:  # noqa: BLE001
        logger.warning("nsworkspace.import_failed")
        return None

    cls = _get_listener_class()
    if cls is None:
        logger.warning("nsworkspace.import_failed")
        return None

    listener = cls.alloc().initWithCallback_(callback)
    nc = NSWorkspace.sharedWorkspace().notificationCenter()
    nc.addObserver_selector_name_object_(
        listener, "onActivate:", "NSWorkspaceDidActivateApplicationNotification", None
    )
    return listener
