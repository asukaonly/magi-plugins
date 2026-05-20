"""Tests for ids helpers."""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType


def _load_module() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "ids.py"
    spec = importlib.util.spec_from_file_location("screenshot_timeline_ids", module_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # required for @dataclass under Python 3.12
    spec.loader.exec_module(mod)
    return mod


def test_new_capture_id_starts_with_prefix() -> None:
    ids = _load_module()
    value = ids.new_capture_id()
    assert value.startswith("cap_")
    assert len(value) == 4 + 26  # "cap_" + 26 ULID chars


def test_new_capture_id_monotonic() -> None:
    ids = _load_module()
    a = ids.new_capture_id(now=1_700_000_000.0)
    time.sleep(0.001)  # ensure later ms
    b = ids.new_capture_id(now=1_700_000_001.0)
    assert b > a  # ULID time-prefix is lexicographically sortable


def test_burst_source_item_id_format() -> None:
    ids = _load_module()
    sid = ids.burst_source_item_id(
        start_unix=1_700_000_000.123,
        app_bundle="com.apple.Safari",
        window_id_hash="a3f1",
    )
    assert sid == "20231114_1700000000_com.apple.Safari_a3f1"
