"""Unit tests for session boundary detection + sqlite persistence."""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType


def _load() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "session_tracker.py"
    spec = importlib.util.spec_from_file_location(
        "screenshot_timeline_session_tracker_test", module_path
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Python 3.13's @dataclass walks sys.modules[cls.__module__] looking
    # for InitVar/ClassVar; must register before exec.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------- SessionStore ----------


def test_store_creates_schema_on_open(tmp_path: Path) -> None:
    mod = _load()
    store = mod.SessionStore(tmp_path / "s.db")
    try:
        assert store.list_recent() == []
    finally:
        store.close()


def test_store_upsert_round_trip(tmp_path: Path) -> None:
    mod = _load()
    store = mod.SessionStore(tmp_path / "s.db")
    try:
        rec = mod.SessionRecord(
            session_id="sess_x",
            started_at=1000.0,
            close_reason="open",
            capture_count=3,
            primary_app="com.example",
            apps_count={"com.example": 3},
            capture_ids=["c1", "c2", "c3"],
        )
        store.upsert(rec)
        loaded = store.list_recent()
        assert len(loaded) == 1
        assert loaded[0].session_id == "sess_x"
        assert loaded[0].capture_count == 3
        assert loaded[0].apps_count == {"com.example": 3}
        assert loaded[0].capture_ids == ["c1", "c2", "c3"]

        # Update in place
        rec.capture_count = 4
        rec.capture_ids.append("c4")
        rec.apps_count["com.example"] = 4
        store.upsert(rec)
        loaded = store.list_recent()
        assert len(loaded) == 1
        assert loaded[0].capture_count == 4
        assert loaded[0].capture_ids == ["c1", "c2", "c3", "c4"]
    finally:
        store.close()


def test_store_find_open_returns_only_open_rows(tmp_path: Path) -> None:
    mod = _load()
    store = mod.SessionStore(tmp_path / "s.db")
    try:
        store.upsert(mod.SessionRecord("sess_a", 100.0, ended_at=110.0, close_reason="idle"))
        store.upsert(mod.SessionRecord("sess_b", 200.0, close_reason="open"))
        store.upsert(mod.SessionRecord("sess_c", 50.0, close_reason="open"))  # older open
        open_row = store.find_open()
        # Latest open by started_at wins
        assert open_row is not None
        assert open_row.session_id == "sess_b"
    finally:
        store.close()


# ---------- SessionTracker ----------


def _make_tracker(tmp_path: Path, idle: float = 60.0):
    mod = _load()
    store = mod.SessionStore(tmp_path / "s.db")
    tracker = mod.SessionTracker(store=store, idle_threshold_seconds=idle)
    return mod, store, tracker


def test_first_capture_opens_a_session(tmp_path: Path) -> None:
    mod, store, tracker = _make_tracker(tmp_path)
    try:
        closed, current = tracker.observe_capture(
            capture_id="c1", captured_at=1000.0,
            app_bundle="com.example", idle_seconds=0.0, screen_locked=False,
        )
        assert closed is None
        assert current.session_id == tracker.open_session_id
        assert current.capture_count == 1
        assert current.capture_ids == ["c1"]
        assert current.primary_app == "com.example"
    finally:
        store.close()


def test_consecutive_active_captures_stay_in_one_session(tmp_path: Path) -> None:
    mod, store, tracker = _make_tracker(tmp_path, idle=60.0)
    try:
        sids = []
        for i in range(5):
            _, cur = tracker.observe_capture(
                capture_id=f"c{i}", captured_at=1000.0 + i * 10,
                app_bundle="com.example", idle_seconds=0.0, screen_locked=False,
            )
            sids.append(cur.session_id)
        assert len(set(sids)) == 1
        assert sids[0] == tracker.open_session_id
    finally:
        store.close()


def test_idle_threshold_closes_previous_session(tmp_path: Path) -> None:
    mod, store, tracker = _make_tracker(tmp_path, idle=60.0)
    try:
        tracker.observe_capture(
            capture_id="c1", captured_at=1000.0,
            app_bundle="com.example", idle_seconds=0.0, screen_locked=False,
        )
        sid1 = tracker.open_session_id

        # Next capture arrives at t=2000 with idle_seconds=120 → user
        # was inactive for the past 2 minutes (>60s threshold), so the
        # previous session should close.
        closed, current = tracker.observe_capture(
            capture_id="c2", captured_at=2000.0,
            app_bundle="com.example", idle_seconds=120.0, screen_locked=False,
        )
        assert closed is not None
        assert closed.session_id == sid1
        assert closed.close_reason == "idle"
        # ended_at should be the inferred last-activity time, NOT now.
        assert abs(closed.ended_at - (2000.0 - 120.0)) < 0.01
        assert current.session_id != sid1
        assert current.capture_ids == ["c2"]
    finally:
        store.close()


def test_screen_lock_closes_previous_session(tmp_path: Path) -> None:
    mod, store, tracker = _make_tracker(tmp_path, idle=60.0)
    try:
        tracker.observe_capture(
            capture_id="c1", captured_at=1000.0,
            app_bundle="com.example", idle_seconds=0.0, screen_locked=False,
        )
        sid1 = tracker.open_session_id
        # screen_locked=True overrides idle math
        closed, current = tracker.observe_capture(
            capture_id="c2", captured_at=1010.0,
            app_bundle="com.example", idle_seconds=2.0, screen_locked=True,
        )
        assert closed is not None
        assert closed.session_id == sid1
        assert closed.close_reason == "lock"
        # For lock, ended_at is the capture time (we don't know exactly
        # when the lock event happened, just that it happened by now)
        assert closed.ended_at == 1010.0
        assert current.session_id != sid1
    finally:
        store.close()


def test_session_spans_multiple_apps_until_idle(tmp_path: Path) -> None:
    """A session is a workflow, not a window. Switching apps without
    going idle keeps the same session."""
    mod, store, tracker = _make_tracker(tmp_path, idle=60.0)
    try:
        apps = ["com.example.A", "com.example.B", "com.example.A", "com.example.C"]
        for i, app in enumerate(apps):
            tracker.observe_capture(
                capture_id=f"c{i}", captured_at=1000.0 + i * 10,
                app_bundle=app, idle_seconds=0.0, screen_locked=False,
            )
        # Single session, 4 captures, primary_app = the most-seen.
        assert tracker._open is not None
        assert tracker._open.capture_count == 4
        assert tracker._open.primary_app == "com.example.A"
    finally:
        store.close()


def test_shutdown_closes_open_session(tmp_path: Path) -> None:
    mod, store, tracker = _make_tracker(tmp_path)
    try:
        tracker.observe_capture(
            capture_id="c1", captured_at=1000.0,
            app_bundle="com.example", idle_seconds=0.0, screen_locked=False,
        )
        sid = tracker.open_session_id
        closed = tracker.shutdown(now=1500.0)
        assert closed is not None
        assert closed.session_id == sid
        assert closed.close_reason == "shutdown"
        assert closed.ended_at == 1500.0
        # In-memory state cleared.
        assert tracker.open_session_id is None
    finally:
        store.close()


def test_recovery_marks_stale_open_session_as_shutdown(tmp_path: Path) -> None:
    """If a previous backend run crashed mid-session, the next start
    should not try to roll forward — clean break + reason=shutdown."""
    mod = _load()
    db_path = tmp_path / "s.db"

    # Simulate previous run: open a session, then "crash" (don't close).
    store1 = mod.SessionStore(db_path)
    t1 = mod.SessionTracker(store=store1, idle_threshold_seconds=60.0)
    t1.observe_capture(
        capture_id="c1", captured_at=1000.0,
        app_bundle="com.example", idle_seconds=0.0, screen_locked=False,
    )
    stale_id = t1.open_session_id
    store1.close()  # no shutdown() — simulates crash

    # New run: fresh tracker should find the stale row and mark it closed.
    store2 = mod.SessionStore(db_path)
    try:
        t2 = mod.SessionTracker(store=store2, idle_threshold_seconds=60.0)
        # The stale row should now be closed.
        all_rows = store2.list_recent()
        stale_row = next(r for r in all_rows if r.session_id == stale_id)
        assert stale_row.close_reason == "shutdown"
        assert stale_row.ended_at is not None
        # No open session in the new tracker.
        assert t2.open_session_id is None
    finally:
        store2.close()


def test_idle_signal_missing_does_not_close_session(tmp_path: Path) -> None:
    """If the helper couldn't read idle (rare), treat as 'no signal' —
    NOT as 'user idle forever'. Closing on missing data would fragment
    every session at the first opaque frame."""
    mod, store, tracker = _make_tracker(tmp_path, idle=60.0)
    try:
        tracker.observe_capture(
            capture_id="c1", captured_at=1000.0,
            app_bundle="com.example", idle_seconds=0.0, screen_locked=False,
        )
        sid1 = tracker.open_session_id
        closed, current = tracker.observe_capture(
            capture_id="c2", captured_at=1010.0,
            app_bundle="com.example", idle_seconds=None, screen_locked=False,
        )
        assert closed is None
        assert current.session_id == sid1
    finally:
        store.close()
