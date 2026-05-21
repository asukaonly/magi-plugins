"""Tests for the trigger orchestrator."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "trigger_orchestrator.py"
    spec = importlib.util.spec_from_file_location("screenshot_timeline_trigger", module_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # required for @dataclass under Python 3.12
    spec.loader.exec_module(mod)
    return mod


def test_global_debounce_filters_close_triggers() -> None:
    mod = _load()
    d = mod.Debouncer(min_interval_seconds=1.5)
    assert d.accept(now=100.0) is True
    assert d.accept(now=100.5) is False
    assert d.accept(now=101.4) is False
    assert d.accept(now=101.6) is True


def test_per_key_debounce_independent() -> None:
    mod = _load()
    d = mod.PerKeyDebouncer(min_interval_seconds=2.0)
    assert d.accept("scroll", now=100.0) is True
    assert d.accept("scroll", now=101.0) is False
    assert d.accept("arrow", now=101.0) is True  # different key
    assert d.accept("scroll", now=102.5) is True


@pytest.mark.asyncio
async def test_timer_emits_on_interval() -> None:
    mod = _load()
    fired: list[float] = []

    async def on_tick(trigger: str) -> None:
        fired.append(len(fired))

    timer = mod.IntervalTimer(interval_seconds=0.05, trigger_label="timer", on_tick=on_tick)
    await timer.start()
    await asyncio.sleep(0.18)
    await timer.stop()
    assert len(fired) >= 3
    assert len(fired) <= 5  # bounded to avoid flakiness


@pytest.mark.asyncio
async def test_emit_routes_through_global_debounce() -> None:
    mod = _load()
    fired: list[str] = []

    async def on_capture(trigger: str) -> None:
        fired.append(trigger)

    orch = mod.TriggerOrchestrator(on_capture=on_capture, global_debounce_seconds=1.5)
    await orch.emit("timer", now=100.0)
    await orch.emit("window_switch", now=100.3)  # filtered
    await orch.emit("keyboard", now=102.0)
    assert fired == ["timer", "keyboard"]
