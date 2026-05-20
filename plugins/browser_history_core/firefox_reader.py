"""Read local Firefox history from places.sqlite."""
from __future__ import annotations

import configparser
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

from .normalizers import (
    burst_merge_key,
    canonicalize_url,
    firefox_time_to_unix_seconds,
    normalize_domain,
    normalize_title,
)
from .visit_merger import aggregate_visits

_IGNORED_VISIT_TYPES = (4, 7, 8)  # EMBED, DOWNLOAD, FRAMED_LINK

DEFAULT_WINDOWS_FIREFOX_ROOT = "~/AppData/Roaming/Mozilla/Firefox"
DEFAULT_MACOS_FIREFOX_ROOT = "~/Library/Application Support/Firefox"
DEFAULT_LINUX_FIREFOX_ROOT = "~/.mozilla/firefox"


def _default_firefox_root() -> str:
    if sys.platform == "win32":
        return DEFAULT_WINDOWS_FIREFOX_ROOT
    if sys.platform == "linux":
        return DEFAULT_LINUX_FIREFOX_ROOT
    return DEFAULT_MACOS_FIREFOX_ROOT


class FirefoxHistoryReader:
    """Read and normalize Mozilla Firefox history visits."""

    def resolve_root(self, source_path: str | None = None) -> Path:
        return Path(source_path or _default_firefox_root()).expanduser()

    def resolve_profile_dir(self, source_path: str | None = None, profile: str = "") -> Path:
        root = self.resolve_root(source_path)
        if (root / "places.sqlite").exists():
            return root

        profile_text = str(profile or "").strip()
        profile_candidates: list[Path] = []
        if profile_text:
            profile_candidates.extend(
                [
                    root / "Profiles" / profile_text,
                    root / profile_text,
                ]
            )

        profile_candidates.extend(self._profiles_from_ini(root))
        for candidate in profile_candidates:
            db_file = candidate / "places.sqlite"
            if db_file.exists():
                return candidate

        profiles_root = root / "Profiles"
        if profiles_root.exists():
            for candidate in sorted(path for path in profiles_root.iterdir() if path.is_dir()):
                if (candidate / "places.sqlite").exists():
                    return candidate

        raise FileNotFoundError(f"Firefox profile not found under: {root}")

    def _profiles_from_ini(self, root: Path) -> list[Path]:
        ini_path = root / "profiles.ini"
        if not ini_path.exists():
            return []

        parser = configparser.ConfigParser(interpolation=None)
        parser.read(ini_path, encoding="utf-8")

        ordered_candidates: list[Path] = []

        for section in parser.sections():
            if not section.startswith("Install"):
                continue
            default_path = parser.get(section, "Default", fallback="").strip()
            if default_path:
                ordered_candidates.append(self._resolve_profile_path(root, default_path, is_relative=True))

        for section in parser.sections():
            if not section.startswith("Profile"):
                continue
            is_default = parser.get(section, "Default", fallback="0").strip() == "1"
            if not is_default:
                continue
            profile_path = parser.get(section, "Path", fallback="").strip()
            is_relative = parser.get(section, "IsRelative", fallback="1").strip() == "1"
            if profile_path:
                ordered_candidates.append(
                    self._resolve_profile_path(root, profile_path, is_relative=is_relative)
                )

        for section in parser.sections():
            if not section.startswith("Profile"):
                continue
            profile_path = parser.get(section, "Path", fallback="").strip()
            is_relative = parser.get(section, "IsRelative", fallback="1").strip() == "1"
            if profile_path:
                ordered_candidates.append(
                    self._resolve_profile_path(root, profile_path, is_relative=is_relative)
                )

        deduped: list[Path] = []
        seen: set[str] = set()
        for path in ordered_candidates:
            key = str(path.resolve()) if path.exists() else str(path)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(path)
        return deduped

    def _resolve_profile_path(self, root: Path, raw_path: str, *, is_relative: bool) -> Path:
        path = Path(raw_path).expanduser()
        if is_relative:
            return root / path
        return path

    def _copy_places_database(self, profile_dir: Path) -> Path:
        places_file = profile_dir / "places.sqlite"
        if not places_file.exists():
            raise FileNotFoundError(f"Firefox places database not found: {places_file}")
        temp_dir = Path(tempfile.mkdtemp(prefix="magi-firefox-history-"))
        copy_path = temp_dir / "places.sqlite"
        shutil.copy2(places_file, copy_path)
        return copy_path

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
        profile_dir = self.resolve_profile_dir(source_path=source_path, profile=profile)
        copy_path = self._copy_places_database(profile_dir)
        try:
            return self._query_visits(
                copy_path=copy_path,
                profile=profile_dir.name,
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
        profile_dir = self.resolve_profile_dir(source_path=source_path, profile=profile)
        copy_path = self._copy_places_database(profile_dir)
        try:
            connection = sqlite3.connect(str(copy_path))
            try:
                cursor = connection.execute("SELECT COALESCE(MAX(id), 0) FROM moz_historyvisits")
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
        ignored_placeholders = ", ".join("?" for _ in _IGNORED_VISIT_TYPES)
        last_visit_id = int(last_cursor) if str(last_cursor or "").isdigit() else 0
        connection = sqlite3.connect(str(copy_path))
        connection.row_factory = sqlite3.Row
        try:
            if last_visit_id > 0:
                new_cursor = connection.execute(
                    f"""
                    SELECT
                        h.id AS visit_id,
                        p.url AS url,
                        p.title AS title,
                        p.visit_count AS visit_count,
                        h.visit_date AS raw_visit_time,
                        h.from_visit AS from_visit,
                        h.visit_type AS transition
                    FROM moz_historyvisits h
                    JOIN moz_places p ON h.place_id = p.id
                    WHERE h.id > ?
                      AND h.visit_type NOT IN ({ignored_placeholders})
                    ORDER BY h.id ASC
                    LIMIT ?
                    """,
                    (last_visit_id, *_IGNORED_VISIT_TYPES, max(1, limit)),
                )
                new_rows = new_cursor.fetchall()
                rows = []
                if new_rows:
                    earliest_new_visit_time = min(int(row["raw_visit_time"] or 0) for row in new_rows)
                    seed_rows: list[sqlite3.Row] = []
                    if merge_window_seconds > 0 and earliest_new_visit_time > 0:
                        merge_window_microseconds = int(max(1.0, merge_window_seconds) * 1_000_000)
                        seed_cursor = connection.execute(
                            f"""
                            SELECT
                                h.id AS visit_id,
                                p.url AS url,
                                p.title AS title,
                                p.visit_count AS visit_count,
                                h.visit_date AS raw_visit_time,
                                h.from_visit AS from_visit,
                                h.visit_type AS transition
                            FROM moz_historyvisits h
                            JOIN moz_places p ON h.place_id = p.id
                            WHERE h.id <= ?
                              AND h.visit_date >= ?
                              AND h.visit_type NOT IN ({ignored_placeholders})
                            ORDER BY h.id ASC
                            """,
                            (last_visit_id, earliest_new_visit_time - merge_window_microseconds, *_IGNORED_VISIT_TYPES),
                        )
                        seed_rows = seed_cursor.fetchall()
                    rows = [*seed_rows, *new_rows]
            elif initial_lookback_hours is not None:
                lookback_microseconds = max(1, initial_lookback_hours) * 3600 * 1_000_000
                cursor = connection.execute(
                    f"""
                    SELECT
                        h.id AS visit_id,
                        p.url AS url,
                        p.title AS title,
                        p.visit_count AS visit_count,
                        h.visit_date AS raw_visit_time,
                        h.from_visit AS from_visit,
                        h.visit_type AS transition
                    FROM moz_historyvisits h
                    JOIN moz_places p ON h.place_id = p.id
                    WHERE h.visit_date >= (
                        SELECT COALESCE(MAX(visit_date), 0) - ?
                        FROM moz_historyvisits
                    )
                      AND h.visit_type NOT IN ({ignored_placeholders})
                    ORDER BY h.id ASC
                    LIMIT ?
                    """,
                    (lookback_microseconds, *_IGNORED_VISIT_TYPES, max(1, limit)),
                )
                rows = cursor.fetchall()
            else:
                cursor = connection.execute(
                    f"""
                    SELECT
                        h.id AS visit_id,
                        p.url AS url,
                        p.title AS title,
                        p.visit_count AS visit_count,
                        h.visit_date AS raw_visit_time,
                        h.from_visit AS from_visit,
                        h.visit_type AS transition
                    FROM moz_historyvisits h
                    JOIN moz_places p ON h.place_id = p.id
                    WHERE h.visit_type NOT IN ({ignored_placeholders})
                    ORDER BY h.id ASC
                    LIMIT ?
                    """,
                    (*_IGNORED_VISIT_TYPES, max(1, limit)),
                )
                rows = cursor.fetchall()
        finally:
            connection.close()

        visits: list[dict[str, Any]] = []
        for row in rows:
            visit_time = firefox_time_to_unix_seconds(row["raw_visit_time"])
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
