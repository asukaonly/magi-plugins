"""Smoke tests for permissions module."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "permissions.py"
    spec = importlib.util.spec_from_file_location("screenshot_timeline_permissions", module_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_screen_recording_status_returns_known_value() -> None:
    mod = _load()
    status = mod.screen_recording_status()
    assert status in {"granted", "denied", "not_determined", "unknown"}


def test_accessibility_status_returns_known_value() -> None:
    mod = _load()
    status = mod.accessibility_status()
    assert status in {"granted", "denied", "not_determined", "unknown"}


def test_all_statuses_shape() -> None:
    mod = _load()
    statuses = mod.all_statuses()
    assert set(statuses.keys()) == {"screen_recording", "accessibility"}
    for v in statuses.values():
        assert v in {"granted", "denied", "not_determined", "unknown"}
