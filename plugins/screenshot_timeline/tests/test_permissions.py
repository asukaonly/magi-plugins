"""Tests for the helper-delegated permissions module."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest


def _load() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "permissions.py"
    spec = importlib.util.spec_from_file_location("screenshot_timeline_permissions", module_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _mock_helper_response(status: str) -> object:
    """Return a CompletedProcess-like object whose stdout has one JSON probe line."""
    stdout = json.dumps({"id": "p", "ok": True, "permission_status": status}) + "\n"
    return subprocess.CompletedProcess(args=["helper"], returncode=0, stdout=stdout, stderr="")


def test_screen_recording_status_returns_granted_when_helper_says_granted() -> None:
    mod = _load()
    with patch.object(mod.subprocess, "run", return_value=_mock_helper_response("granted")):
        assert mod.screen_recording_status() == "granted"


def test_screen_recording_status_returns_denied_when_helper_says_denied() -> None:
    mod = _load()
    with patch.object(mod.subprocess, "run", return_value=_mock_helper_response("denied")):
        assert mod.screen_recording_status() == "denied"


def test_accessibility_status_returns_granted() -> None:
    mod = _load()
    with patch.object(mod.subprocess, "run", return_value=_mock_helper_response("granted")):
        assert mod.accessibility_status() == "granted"


def test_helper_unknown_status_collapses_to_unknown() -> None:
    mod = _load()
    stdout = json.dumps({"id": "p", "ok": True, "permission_status": "weird"}) + "\n"
    mock_proc = subprocess.CompletedProcess(args=["helper"], returncode=0, stdout=stdout, stderr="")
    with patch.object(mod.subprocess, "run", return_value=mock_proc):
        assert mod.screen_recording_status() == "unknown"


def test_helper_error_response_returns_unknown() -> None:
    mod = _load()
    stdout = json.dumps({"id": "p", "ok": False, "error": {"code": "BOOM", "message": ""}}) + "\n"
    mock_proc = subprocess.CompletedProcess(args=["helper"], returncode=1, stdout=stdout, stderr="")
    with patch.object(mod.subprocess, "run", return_value=mock_proc):
        assert mod.screen_recording_status() == "unknown"


def test_helper_timeout_returns_unknown() -> None:
    mod = _load()
    with patch.object(mod.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1)):
        assert mod.screen_recording_status() == "unknown"


def test_helper_missing_returns_unknown(monkeypatch) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "_helper_binary_path", lambda: None)
    assert mod.screen_recording_status() == "unknown"


def test_all_statuses_shape() -> None:
    mod = _load()
    with patch.object(mod.subprocess, "run", return_value=_mock_helper_response("granted")):
        s = mod.all_statuses()
        assert set(s.keys()) == {"screen_recording", "accessibility"}
        assert all(v == "granted" for v in s.values())
