"""Read local Chromium history from profile SQLite databases."""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .normalizers import (
    burst_merge_key,
    canonicalize_url,
    chrome_time_to_unix_seconds,
    normalize_domain,
    normalize_title,
)
from .visit_merger import aggregate_visits


class ChromiumHistoryReader:
    """Read and normalize Chromium browser history visits."""

    def __init__(self, *, default_root: str, browser_label: str = "Chromium") -> None:
        self._default_root = default_root
        self._browser_label = browser_label

    def resolve_root(self, source_path: str | None = None) -> Path:
        root = Path(source_path or self._default_root).expanduser()
        return root

    def resolve_profile_dir(self, source_path: str | None = None, profile: str = "Default") -> Path:
        root = self.resolve_root(source_path)
        candidate = root / profile
        if candidate.exists():
            return candidate
        raise FileNotFoundError(f"{self._browser_label} profile not found: {candidate}")

    def _copy_history_database(self, profile_dir: Path) -> Path:
        history_file = profile_dir / "History"
        if not history_file.exists():
            raise FileNotFoundError(f"{self._browser_label} history database not found: {history_file}")
        temp_dir = Path(tempfile.mkdtemp(prefix=f"magi-{self._browser_label.lower()}-history-"))
        copy_path = temp_dir / "History"
        shutil.copy2(history_file, copy_path)
        return copy_path

    def read_visits(
        self,
        *,
        source_path: str | None = None,
        profile: str = "Default",
        limit: int = 200,
        last_cursor: str | None = None,
        initial_lookback_hours: int | None = 24,
        merge_window_seconds: float = 30 * 60.0,
    ) -> list[dict[str, Any]]:
        profile_dir = self.resolve_profile_dir(source_path=source_path, profile=profile)
        copy_path = self._copy_history_database(profile_dir)
        try:
            return self._query_visits(
                copy_path=copy_path,
                profile=profile,
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
        profile: str = "Default",
    ) -> int:
        profile_dir = self.resolve_profile_dir(source_path=source_path, profile=profile)
        copy_path = self._copy_history_database(profile_dir)
        try:
            connection = sqlite3.connect(str(copy_path))
            try:
                cursor = connection.execute("SELECT COALESCE(MAX(id), 0) FROM visits")
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
        profile: str,
        limit: int,
        last_cursor: str | None,
        initial_lookback_hours: int | None,
        merge_window_seconds: float,
    ) -> list[dict[str, Any]]:
        last_visit_id = int(last_cursor) if str(last_cursor or "").isdigit() else 0
        connection = sqlite3.connect(str(copy_path))
        connection.row_factory = sqlite3.Row
        try:
            if last_visit_id > 0:
                new_cursor = connection.execute(
                    """
                    SELECT
                        visits.id AS visit_id,
                        urls.url AS url,
                        urls.title AS title,
                        urls.visit_count AS visit_count,
                        visits.visit_time AS raw_visit_time,
                        visits.from_visit AS from_visit,
                        visits.transition AS transition
                    FROM visits
                    JOIN urls ON visits.url = urls.id
                    WHERE visits.id > ?
                      AND (visits.transition & 255) != 3
                      AND (visits.transition & 536870912) != 0
                    ORDER BY visits.id ASC
                    LIMIT ?
                    """,
                    (last_visit_id, max(1, limit)),
                )
                new_rows = new_cursor.fetchall()
                rows = []
                if new_rows:
                    earliest_new_visit_time = min(int(row["raw_visit_time"] or 0) for row in new_rows)
                    seed_rows: list[sqlite3.Row] = []
                    if merge_window_seconds > 0 and earliest_new_visit_time > 0:
                        merge_window_microseconds = int(max(1.0, merge_window_seconds) * 1_000_000)
                        seed_cursor = connection.execute(
                            """
                            SELECT
                                visits.id AS visit_id,
                                urls.url AS url,
                                urls.title AS title,
                                urls.visit_count AS visit_count,
                                visits.visit_time AS raw_visit_time,
                                visits.from_visit AS from_visit,
                                visits.transition AS transition
                            FROM visits
                            JOIN urls ON visits.url = urls.id
                            WHERE visits.id <= ?
                              AND visits.visit_time >= ?
                              AND (visits.transition & 255) != 3
                              AND (visits.transition & 536870912) != 0
                            ORDER BY visits.id ASC
                            """,
                            (last_visit_id, earliest_new_visit_time - merge_window_microseconds),
                        )
                        seed_rows = seed_cursor.fetchall()
                    rows = [*seed_rows, *new_rows]
            elif initial_lookback_hours is not None:
                lookback_microseconds = max(1, initial_lookback_hours) * 3600 * 1_000_000
                cursor = connection.execute(
                    """
                    SELECT
                        visits.id AS visit_id,
                        urls.url AS url,
                        urls.title AS title,
                        urls.visit_count AS visit_count,
                        visits.visit_time AS raw_visit_time,
                        visits.from_visit AS from_visit,
                        visits.transition AS transition
                    FROM visits
                    JOIN urls ON visits.url = urls.id
                    WHERE visits.visit_time >= (
                        SELECT COALESCE(MAX(visit_time), 0) - ?
                        FROM visits
                    )
                      AND (visits.transition & 255) != 3
                      AND (visits.transition & 536870912) != 0
                    ORDER BY visits.id ASC
                    LIMIT ?
                    """,
                    (lookback_microseconds, max(1, limit)),
                )
                rows = cursor.fetchall()
            else:
                cursor = connection.execute(
                    """
                    SELECT
                        visits.id AS visit_id,
                        urls.url AS url,
                        urls.title AS title,
                        urls.visit_count AS visit_count,
                        visits.visit_time AS raw_visit_time,
                        visits.from_visit AS from_visit,
                        visits.transition AS transition
                    FROM visits
                    JOIN urls ON visits.url = urls.id
                    WHERE (visits.transition & 255) != 3
                      AND (visits.transition & 536870912) != 0
                    ORDER BY visits.id ASC
                    LIMIT ?
                    """,
                    (max(1, limit),),
                )
                rows = cursor.fetchall()
        finally:
            connection.close()

        visits: list[dict[str, Any]] = []
        for row in rows:
            visit_time = chrome_time_to_unix_seconds(row["raw_visit_time"])
            url = str(row["url"] or "")
            visit_id = int(row["visit_id"] or 0)
            visits.append(
                {
                    "visit_id": str(visit_id),
                    "url": url,
                    "canonical_url": canonicalize_url(url),
                    "burst_merge_key": burst_merge_key(url, row["title"]),
                    "title": str(row["title"] or ""),
                    "normalized_title": normalize_title(row["title"]),
                    "visit_time": visit_time,
                    "visit_count": int(row["visit_count"] or 0),
                    "from_visit": str(row["from_visit"] or ""),
                    "transition": str(row["transition"] or ""),
                    "profile": profile,
                    "domain": normalize_domain(url),
                    "_is_new_visit": last_visit_id <= 0 or visit_id > last_visit_id,
                }
            )
        return aggregate_visits(
            visits,
            merge_window_seconds=merge_window_seconds,
        )
