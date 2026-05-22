"""Tests for the dHash hamming-distance helper."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "phash_utils.py"
    spec = importlib.util.spec_from_file_location("screenshot_timeline_phash_utils", module_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_identical_hashes_have_distance_zero() -> None:
    h = _load().hamming_distance
    assert h("a4bcb89cb43830b0", "a4bcb89cb43830b0") == 0


def test_one_bit_flip() -> None:
    """0xa4bc... vs 0xa4bd... — last hex digit c→d toggles one bit."""
    h = _load().hamming_distance
    assert h("a4bcb89cb43830bc", "a4bcb89cb43830bd") == 1


def test_completely_different_hashes_have_high_distance() -> None:
    h = _load().hamming_distance
    # 0xffff...ffff XOR 0x0000...0000 = all 64 bits set
    assert h("ffffffffffffffff", "0000000000000000") == 64


def test_malformed_input_returns_max_so_we_keep_capture() -> None:
    """Caller treats `64` as 'no dedup signal' — we never drop a real capture
    just because we couldn't parse the hash."""
    h = _load().hamming_distance
    assert h("", "a4bcb89cb43830b0") == 64
    assert h("too_short", "a4bcb89cb43830b0") == 64
    assert h("nothexgarbage___", "a4bcb89cb43830b0") == 64
    assert h(None, "a4bcb89cb43830b0") == 64  # type: ignore[arg-type]
