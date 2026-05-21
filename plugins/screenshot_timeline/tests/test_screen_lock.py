"""Smoke test for the screen_lock module."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "screen_lock.py"
    spec = importlib.util.spec_from_file_location("screenshot_timeline_screen_lock", module_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_is_screen_locked_returns_bool() -> None:
    mod = _load()
    result = mod.is_screen_locked()
    assert isinstance(result, bool)
    # We don't know the actual lock state in CI/dev; assert only the type.


def test_handles_quartz_import_failure(monkeypatch) -> None:
    mod = _load()
    # Simulate Quartz not being available
    monkeypatch.setitem(sys.modules, "Quartz", None)
    # Trick: cause the inner `from Quartz import ...` to raise by deleting the symbol path.
    # Simpler: just ensure the function does not raise regardless of environment.
    # (The function already catches BaseException via Exception, so this just exercises the happy path.)
    result = mod.is_screen_locked()
    assert isinstance(result, bool)
