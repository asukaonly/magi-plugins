"""Read local Safari history from History.db."""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from errno import EACCES, EPERM
from pathlib import Path
from typing import Any

_CORE_PARENT = Path(__file__).resolve().parents[1]
if str(_CORE_PARENT) not in sys.path:
    sys.path.append(str(_CORE_PARENT))

from browser_history_core.normalizers import (
    burst_merge_key,
    canonicalize_url,
    normalize_domain,
    normalize_title,
)
from browser_history_core.visit_merger import aggregate_visits

SAFARI_UNIX_OFFSET_SECONDS = 978_307_200
DEFAULT_MACOS_SAFARI_ROOT = "~/Library/Safari"


class SafariHistoryPermissionError(PermissionError):
    """Raised when macOS privacy controls block Safari history access."""


def _default_safari_root() -> str:
    return DEFAULT_MACOS_SAFARI_ROOT


def safari_time_to_unix_seconds(value: int | float | str | None) -> float:
    """Convert Safari seconds since 2001-01-01 into Unix seconds."""

    if value in (None, "", 0, "0"):
        return 0.0
    return max(0.0, float(value) + SAFARI_UNIX_OFFSET_SECONDS)


class SafariHistoryReader:
    """Read and normalize Safari browser history visits."""

    def resolve_root(self, source_path: str | None = None) -> Path:
        return Path(source_path or _default_safari_root()).expanduser()

    def _copy_history_database(self, root: Path) -> Path:
        history_file = root / "History.db"
        try:
            history_file.stat()
        except FileNotFoundError:
            raise FileNotFoundError(f"Safari history database not found: {history_file}")
        except PermissionError as exc:
            raise _permission_error(history_file, exc) from exc
        except OSError as exc:
            if exc.errno in {EACCES, EPERM}:
                raise _permission_error(history_file, exc) from exc
            raise
        temp_dir = Path(tempfile.mkdtemp(prefix="magi-safari-history-"))
        copy_path = temp_dir / "History.db"
        try:
            shutil.copy2(history_file, copy_path)
            return copy_path
        except PermissionError as exc:
            raise _permission_error(history_file, exc) from exc
        except OSError as exc:
            if exc.errno in {EACCES, EPERM}:
                raise _permission_error(history_file, exc) from exc
            raise

    def read_visits(
        self,
        *,
        source_path: str | None = None,
        profile: str = "",
        limit: int = 200,
        last_cursor: str | None = None,
        initial_lookback_hours: int | None = 24,
        merge_window_seconds: float = 30 * 60.0,
    ) -> list[dict[str, Any]]:
        del profile
        root = self.resolve_root(source_path)
        copy_path = self._copy_history_database(root)
        try:
            return self._query_visits(
                copy_path=copy_path,
                limit=limit,
                last_cursor=last_cursor,
                initial_lookback_hours=initial_lookback_hours,
                merge_window_seconds=merge_window_seconds,
            )
        finally:
            shutil.rmtree(copy_path.parent, ignore_errors=True)

    def get_latest_visit_id(
        self,
        *,
        source_path: str | None = None,
        profile: str = "",
    ) -> int:
        del profile
        root = self.resolve_root(source_path)
        copy_path = self._copy_history_database(root)
        try:
            connection = sqlite3.connect(str(copy_path))
            try:
                cursor = connection.execute("SELECT COALESCE(MAX(id), 0) FROM history_visits")
                row = cursor.fetchone()
                return int(row[0] or 0) if row else 0
            finally:
                connection.close()
        finally:
            shutil.rmtree(copy_path.parent, ignore_errors=True)

    def _query_visits(
        self,
        *,
        copy_path: Path,
        limit: int,
        last_cursor: str | None,
        initial_lookback_hours: int | None,
        merge_window_seconds: float,
    ) -> list[dict[str, Any]]:
        last_visit_id = int(last_cursor) if str(last_cursor or "").isdigit() else 0
        connection = sqlite3.connect(str(copy_path))
        connection.row_factory = sqlite3.Row
        try:
            visit_columns = _table_columns(connection, "history_visits")
            filter_sql = _visit_filter_sql(visit_columns)
            if last_visit_id > 0:
                new_cursor = connection.execute(
                    f"""
                    SELECT
                        v.id AS visit_id,
                        i.url AS url,
                        v.title AS title,
                        i.visit_count AS visit_count,
                        v.visit_time AS raw_visit_time
                    FROM history_visits v
                    JOIN history_items i ON v.history_item = i.id
                    WHERE v.id > ?
                      {filter_sql}
                    ORDER BY v.id ASC
                    LIMIT ?
                    """,
                    (last_visit_id, max(1, limit)),
                )
                new_rows = new_cursor.fetchall()
                rows: list[sqlite3.Row] = []
                if new_rows:
                    earliest_new_visit_time = min(float(row["raw_visit_time"] or 0) for row in new_rows)
                    seed_rows: list[sqlite3.Row] = []
                    if merge_window_seconds > 0 and earliest_new_visit_time > 0:
                        seed_cursor = connection.execute(
                            f"""
                            SELECT
                                v.id AS visit_id,
                                i.url AS url,
                                v.title AS title,
                                i.visit_count AS visit_count,
                                v.visit_time AS raw_visit_time
                            FROM history_visits v
                            JOIN history_items i ON v.history_item = i.id
                            WHERE v.id <= ?
                              AND v.visit_time >= ?
                              {filter_sql}
                            ORDER BY v.id ASC
                            """,
                            (last_visit_id, earliest_new_visit_time - float(merge_window_seconds)),
                        )
                        seed_rows = seed_cursor.fetchall()
                    rows = [*seed_rows, *new_rows]
            elif initial_lookback_hours is not None:
                lookback_seconds = max(1, initial_lookback_hours) * 3600
                cursor = connection.execute(
                    f"""
                    SELECT
                        v.id AS visit_id,
                        i.url AS url,
                        v.title AS title,
                        i.visit_count AS visit_count,
                        v.visit_time AS raw_visit_time
                    FROM history_visits v
                    JOIN history_items i ON v.history_item = i.id
                    WHERE v.visit_time >= (
                        SELECT COALESCE(MAX(visit_time), 0) - ?
                        FROM history_visits
                    )
                      {filter_sql}
                    ORDER BY v.id ASC
                    LIMIT ?
                    """,
                    (lookback_seconds, max(1, limit)),
                )
                rows = cursor.fetchall()
            else:
                cursor = connection.execute(
                    f"""
                    SELECT
                        v.id AS visit_id,
                        i.url AS url,
                        v.title AS title,
                        i.visit_count AS visit_count,
                        v.visit_time AS raw_visit_time
                    FROM history_visits v
                    JOIN history_items i ON v.history_item = i.id
                    WHERE 1 = 1
                      {filter_sql}
                    ORDER BY v.id ASC
                    LIMIT ?
                    """,
                    (max(1, limit),),
                )
                rows = cursor.fetchall()
        finally:
            connection.close()

        visits: list[dict[str, Any]] = []
        for row in rows:
            visit_time = safari_time_to_unix_seconds(row["raw_visit_time"])
            url = str(row["url"] or "")
            title = str(row["title"] or "")
            visit_id = int(row["visit_id"] or 0)
            visits.append(
                {
                    "visit_id": str(visit_id),
                    "url": url,
                    "canonical_url": canonicalize_url(url),
                    "burst_merge_key": burst_merge_key(url, title),
                    "title": title,
                    "normalized_title": normalize_title(title),
                    "visit_time": visit_time,
                    "visit_count": int(row["visit_count"] or 0),
                    "from_visit": "",
                    "transition": "",
                    "profile": "Safari",
                    "domain": normalize_domain(url),
                    "_is_new_visit": last_visit_id <= 0 or visit_id > last_visit_id,
                }
            )
        return aggregate_visits(visits, merge_window_seconds=merge_window_seconds)


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _visit_filter_sql(visit_columns: set[str]) -> str:
    clauses: list[str] = []
    if "load_successful" in visit_columns:
        clauses.append("COALESCE(v.load_successful, 1) = 1")
    if "http_non_get" in visit_columns:
        clauses.append("COALESCE(v.http_non_get, 0) = 0")
    if "synthesized" in visit_columns:
        clauses.append("COALESCE(v.synthesized, 0) = 0")
    if not clauses:
        return ""
    return "AND " + " AND ".join(clauses)


def _permission_error(history_file: Path, exc: BaseException) -> SafariHistoryPermissionError:
    return SafariHistoryPermissionError(
        "Safari History.db is protected by macOS Full Disk Access. "
        "Grant Full Disk Access to Magi (and its sidecar/helper if macOS lists it) in "
        "System Settings > Privacy & Security > Full Disk Access, then fully restart Magi. "
        f"Path: {history_file}"
    )
