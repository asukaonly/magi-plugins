"""Plugin-private session boundary detection + SQLite persistence.

A *session* in this plugin's vocabulary is a contiguous stretch of user
activity — many screenshots from possibly several different apps, but
without an idle gap or a screen lock in between. Sessions are the
analysis unit a future L3 LLM pass would chew on (one summary per
session, then a daily aggregate of summaries).

This module is intentionally self-contained:

  - SQLite db lives at ``~/.magi/data/plugins/screenshot_timeline/sessions.db``,
    NOT in the host's memory store. The host knows nothing about
    sessions — if/when we extract structured information from a
    session, that flows back to host KG via the existing
    SensorOutput.metadata.entities path.
  - We never poll on a timer to detect "user went idle". The signal
    comes for free with every screenshot via the helper's
    ``idle_seconds`` field. When the next capture arrives, we look at
    how idle the user was, and close the previous session retroactively
    if idle exceeded the threshold.
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Idle seconds before we declare the previous session closed. 5 minutes
# is a working compromise: short enough that a meeting break or coffee
# run cleanly partitions the day; long enough that staring at a long
# article without scrolling doesn't fragment a reading session.
DEFAULT_IDLE_THRESHOLD_SECONDS = 5 * 60.0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    started_at        REAL    NOT NULL,
    ended_at          REAL,
    close_reason      TEXT    NOT NULL,
    capture_count     INTEGER NOT NULL DEFAULT 0,
    primary_app       TEXT    NOT NULL DEFAULT '',
    apps_json         TEXT    NOT NULL DEFAULT '{}',
    capture_ids_json  TEXT    NOT NULL DEFAULT '[]',
    -- B/C-phase columns: reserved for future LLM analysis. A-phase
    -- writes 'pending' status and never touches the rest.
    analysis_status   TEXT    NOT NULL DEFAULT 'pending',
    analysis_at       REAL,
    analysis_model    TEXT,
    title             TEXT,
    summary           TEXT,
    topics_json       TEXT,
    entities_json     TEXT,
    worth_memory      INTEGER NOT NULL DEFAULT 0,
    created_at        REAL    NOT NULL,
    updated_at        REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_analysis_status ON sessions(analysis_status);
CREATE INDEX IF NOT EXISTS idx_sessions_close_reason ON sessions(close_reason);
"""


# Possible close reasons. 'open' is the in-flight state, never the
# final value — but persisted so a crashed backend leaves a forensic
# trail that the next boot's _recover_open_session can detect.
CLOSE_REASONS = frozenset({"open", "lock", "idle", "shutdown"})


@dataclass
class SessionRecord:
    """In-memory view of one row in the sessions table."""

    session_id: str
    started_at: float
    ended_at: float | None = None
    close_reason: str = "open"
    capture_count: int = 0
    primary_app: str = ""
    apps_count: dict[str, int] = field(default_factory=dict)
    capture_ids: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float | None:
        if self.ended_at is None:
            return None
        return max(0.0, self.ended_at - self.started_at)


def _new_session_id(now: float) -> str:
    """ULID-style timestamp-sortable id; reuses the project's ids.py
    convention but without the trailing random tail (sessions are
    coarser than captures)."""
    seconds = int(now)
    local = time.localtime(seconds)
    prefix = time.strftime("%Y%m%dT%H%M%S", local)
    # 4-char random tail to keep ids unique on rapid lock-cycle.
    crockford = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    tail = "".join(crockford[b % 32] for b in secrets.token_bytes(4))[:4]
    return f"sess_{prefix}_{tail}"


class SessionStore:
    """SQLite-backed session row persistence. Threadsafe via a single
    connection per instance + a re-entrant lock (the plugin only writes
    from the asyncio main thread, but tests run in sync contexts)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because asyncio + APScheduler may dispatch
        # on different threads; we guard mutations with an explicit lock
        # higher up (SessionTracker.observe_capture is awaited serially).
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def upsert(self, record: SessionRecord) -> None:
        """Insert or replace a session row from the in-memory record."""
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO sessions (
                session_id, started_at, ended_at, close_reason,
                capture_count, primary_app, apps_json, capture_ids_json,
                analysis_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                ended_at = excluded.ended_at,
                close_reason = excluded.close_reason,
                capture_count = excluded.capture_count,
                primary_app = excluded.primary_app,
                apps_json = excluded.apps_json,
                capture_ids_json = excluded.capture_ids_json,
                updated_at = excluded.updated_at
            """,
            (
                record.session_id,
                record.started_at,
                record.ended_at,
                record.close_reason,
                record.capture_count,
                record.primary_app,
                json.dumps(record.apps_count, ensure_ascii=False),
                json.dumps(record.capture_ids),
                now,
                now,
            ),
        )
        self._conn.commit()

    def find_open(self) -> SessionRecord | None:
        """Return the most recent session whose close_reason is still
        ``'open'`` — i.e. the backend didn't get to close it cleanly
        before exit. Used for crash recovery on next start."""
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE close_reason = 'open' "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def mark_close(
        self, session_id: str, *, ended_at: float, close_reason: str
    ) -> None:
        """Force-close a session row by id without rewriting the rest."""
        if close_reason not in CLOSE_REASONS:
            raise ValueError(f"unknown close_reason {close_reason!r}")
        self._conn.execute(
            "UPDATE sessions SET ended_at=?, close_reason=?, updated_at=? "
            "WHERE session_id=?",
            (ended_at, close_reason, time.time(), session_id),
        )
        self._conn.commit()

    def list_recent(self, *, limit: int = 50) -> list[SessionRecord]:
        rows = self._conn.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def _row_to_record(self, row: tuple) -> SessionRecord:
        # Column order matches CREATE TABLE above.
        (
            session_id, started_at, ended_at, close_reason,
            capture_count, primary_app, apps_json, capture_ids_json,
            _analysis_status, _analysis_at, _analysis_model,
            _title, _summary, _topics_json, _entities_json, _worth_memory,
            _created_at, _updated_at,
        ) = row
        try:
            apps = json.loads(apps_json) if apps_json else {}
        except json.JSONDecodeError:
            apps = {}
        try:
            cap_ids = json.loads(capture_ids_json) if capture_ids_json else []
        except json.JSONDecodeError:
            cap_ids = []
        return SessionRecord(
            session_id=session_id,
            started_at=float(started_at),
            ended_at=float(ended_at) if ended_at is not None else None,
            close_reason=str(close_reason),
            capture_count=int(capture_count),
            primary_app=str(primary_app or ""),
            apps_count={str(k): int(v) for k, v in apps.items()},
            capture_ids=[str(c) for c in cap_ids],
        )


class SessionTracker:
    """Decides when a session ends and persists state.

    Call ``observe_capture()`` after every screenshot. The tracker will:

      - close the open session (if any) when the capture's
        ``idle_seconds`` exceeds the threshold OR the screen was locked
        between captures;
      - open a new session if none is open;
      - append the capture to whatever session is now current.

    Call ``shutdown()`` from sensor.stop() to gracefully close the
    in-flight session as ``shutdown``.

    Crash recovery: on first observe, if the DB contains a session with
    close_reason='open' from a previous backend run, we mark it closed
    with reason='shutdown' (we can't tell when it actually ended).
    """

    def __init__(
        self,
        *,
        store: SessionStore,
        idle_threshold_seconds: float = DEFAULT_IDLE_THRESHOLD_SECONDS,
    ) -> None:
        self.store = store
        self.idle_threshold = float(idle_threshold_seconds)
        self._open: SessionRecord | None = None
        self._recover_stale_open_session()

    def _recover_stale_open_session(self) -> None:
        """If a previous backend run died mid-session, close its row.

        We DON'T try to resume — the gap between the previous backend
        exit and now is unknown (could be seconds, could be days). A
        clean break is safer than rolling forward stale state.
        """
        stale = self.store.find_open()
        if stale is None:
            return
        logger.info(
            "session.recover_stale id=%s started_at=%s capture_count=%d",
            stale.session_id, stale.started_at, stale.capture_count,
        )
        self.store.mark_close(
            stale.session_id,
            ended_at=time.time(),
            close_reason="shutdown",
        )

    def observe_capture(
        self,
        *,
        capture_id: str,
        captured_at: float,
        app_bundle: str,
        idle_seconds: float | None,
        screen_locked: bool,
    ) -> tuple[SessionRecord | None, SessionRecord]:
        """Process one capture's session-tracking signals.

        Returns ``(closed_or_none, current_open_session)``. The closed
        session, when present, is the one that just ended and is what a
        downstream B-phase LLM worker would pick up.
        """
        close_reason = self._evaluate_close(idle_seconds, screen_locked)
        closed: SessionRecord | None = None
        if close_reason is not None and self._open is not None:
            # Compute a more accurate end timestamp: if the user was
            # idle for N seconds at the moment of the new capture, then
            # their actual last activity (and thus session end) was N
            # seconds *before* the new capture, not now.
            if close_reason == "idle" and idle_seconds is not None:
                actual_end = max(self._open.started_at, captured_at - float(idle_seconds))
            else:
                actual_end = captured_at
            closed = self._close_current(actual_end, close_reason)

        if self._open is None:
            self._open_new(captured_at)
        assert self._open is not None
        self._append_capture(capture_id, app_bundle)
        self.store.upsert(self._open)
        return closed, self._open

    def shutdown(self, *, now: float | None = None) -> SessionRecord | None:
        """Close any open session at sensor stop. Called once."""
        if self._open is None:
            return None
        return self._close_current(now if now is not None else time.time(), "shutdown")

    @property
    def open_session_id(self) -> str | None:
        return self._open.session_id if self._open else None

    # -------- internals --------

    def _evaluate_close(
        self, idle_seconds: float | None, screen_locked: bool
    ) -> str | None:
        if self._open is None:
            return None
        if screen_locked:
            return "lock"
        if idle_seconds is not None and idle_seconds >= self.idle_threshold:
            return "idle"
        return None

    def _open_new(self, started_at: float) -> None:
        sid = _new_session_id(started_at)
        self._open = SessionRecord(
            session_id=sid,
            started_at=started_at,
            close_reason="open",
        )
        logger.info("session.open id=%s started_at=%s", sid, started_at)

    def _close_current(self, ended_at: float, reason: str) -> SessionRecord:
        assert self._open is not None
        if reason not in CLOSE_REASONS or reason == "open":
            raise ValueError(f"invalid close reason {reason!r}")
        self._open.ended_at = ended_at
        self._open.close_reason = reason
        # Recompute primary_app from apps_count (the running tally
        # might have been wrong if app priority shifted late).
        if self._open.apps_count:
            self._open.primary_app = max(
                self._open.apps_count.items(), key=lambda kv: kv[1]
            )[0]
        self.store.upsert(self._open)
        logger.info(
            "session.close id=%s reason=%s captures=%d duration_s=%.1f primary_app=%s",
            self._open.session_id, reason, self._open.capture_count,
            self._open.duration_seconds or 0.0, self._open.primary_app,
        )
        closed = self._open
        self._open = None
        return closed

    def _append_capture(self, capture_id: str, app_bundle: str) -> None:
        assert self._open is not None
        self._open.capture_ids.append(capture_id)
        self._open.capture_count += 1
        if app_bundle:
            self._open.apps_count[app_bundle] = (
                self._open.apps_count.get(app_bundle, 0) + 1
            )
            if not self._open.primary_app:
                self._open.primary_app = app_bundle


__all__ = [
    "SessionRecord",
    "SessionStore",
    "SessionTracker",
    "DEFAULT_IDLE_THRESHOLD_SECONDS",
    "CLOSE_REASONS",
]
