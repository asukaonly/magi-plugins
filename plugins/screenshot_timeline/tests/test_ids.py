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


def test_new_capture_id_format_is_iso_prefixed() -> None:
    """Capture id must start with a YYYYMMDDTHHMMSS timestamp + microseconds
    + 4-char random tail. Total length is fixed at 26 chars (same as a
    ULID, but human-readable)."""
    ids = _load_module()
    value = ids.new_capture_id()
    # YYYYMMDDTHHMMSS = 15 chars; _ = 1; microseconds = 6; _ = 1; tail = 4.
    assert len(value) == 15 + 1 + 6 + 1 + 4 == 27
    # ISO-ish date-time prefix: year digits then 'T' separator
    assert value[8] == "T"
    assert value[15] == "_"
    assert value[22] == "_"
    # Microseconds block is six digits
    assert value[16:22].isdigit()


def test_new_capture_id_monotonic() -> None:
    """Lexicographic order must equal chronological order so that
    ``ls -1`` / glob results are inherently time-sorted (same property as
    the previous ULID layout)."""
    ids = _load_module()
    a = ids.new_capture_id(now=1_700_000_000.0)
    b = ids.new_capture_id(now=1_700_000_001.0)
    assert b > a


def test_burst_source_item_id_format() -> None:
    ids = _load_module()
    sid = ids.burst_source_item_id(
        start_unix=1_700_000_000.123,
        app_bundle="com.apple.Safari",
        window_id_hash="a3f1",
    )
    assert sid == "20231114_1700000000_com.apple.Safari_a3f1"
