from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest


SAFARI_UNIX_OFFSET = 978_307_200


def _load_safari_reader():
    module_path = Path(__file__).resolve().parents[1] / "safari_reader.py"
    spec = importlib.util.spec_from_file_location("safari_history_reader", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_history_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE history_items (
                id INTEGER PRIMARY KEY,
                url TEXT NOT NULL,
                visit_count INTEGER
            );
            CREATE TABLE history_visits (
                id INTEGER PRIMARY KEY,
                history_item INTEGER NOT NULL,
                visit_time REAL NOT NULL,
                title TEXT,
                load_successful INTEGER,
                http_non_get INTEGER,
                synthesized INTEGER
            );
            """
        )
        connection.execute(
            "INSERT INTO history_items (id, url, visit_count) VALUES (1, ?, 2)",
            ("https://example.com/read",),
        )
        connection.execute(
            """
            INSERT INTO history_visits
                (id, history_item, visit_time, title, load_successful, http_non_get, synthesized)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (10, 1, 1_700_000_000 - SAFARI_UNIX_OFFSET, "Example Article", 1, 0, 0),
        )
        connection.execute(
            """
            INSERT INTO history_visits
                (id, history_item, visit_time, title, load_successful, http_non_get, synthesized)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (11, 1, 1_700_000_100 - SAFARI_UNIX_OFFSET, "POST should be ignored", 1, 1, 0),
        )
        connection.commit()
    finally:
        connection.close()


def test_safari_reader_reads_history_db_and_filters_non_get(tmp_path: Path) -> None:
    reader_module = _load_safari_reader()
    reader = reader_module.SafariHistoryReader()
    root = tmp_path / "Safari"
    root.mkdir()
    _write_history_db(root / "History.db")

    items = reader.read_visits(
        source_path=str(root),
        profile="",
        limit=20,
        initial_lookback_hours=None,
        merge_window_seconds=60.0,
    )

    assert len(items) == 1
    assert items[0]["visit_id"] == "10"
    assert items[0]["title"] == "Example Article"
    assert items[0]["domain"] == "example.com"
    assert abs(float(items[0]["visit_time"]) - 1_700_000_000) < 1
    assert reader.get_latest_visit_id(source_path=str(root), profile="") == 11


def test_safari_reader_cursor_returns_only_new_visits(tmp_path: Path) -> None:
    reader_module = _load_safari_reader()
    reader = reader_module.SafariHistoryReader()
    root = tmp_path / "Safari"
    root.mkdir()
    _write_history_db(root / "History.db")

    items = reader.read_visits(
        source_path=str(root),
        profile="",
        limit=20,
        last_cursor="10",
        initial_lookback_hours=None,
        merge_window_seconds=60.0,
    )

    assert items == []


def test_safari_reader_permission_error_explains_full_disk_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader_module = _load_safari_reader()
    reader = reader_module.SafariHistoryReader()
    root = tmp_path / "Safari"
    root.mkdir()
    history_db = root / "History.db"
    history_db.write_bytes(b"sqlite")

    def blocked_copy(src: Path, dst: Path) -> None:
        raise PermissionError(1, "Operation not permitted", str(src))

    monkeypatch.setattr(reader_module.shutil, "copy2", blocked_copy)

    with pytest.raises(reader_module.SafariHistoryPermissionError) as exc:
        reader.read_visits(
            source_path=str(root),
            profile="",
            limit=20,
            initial_lookback_hours=None,
            merge_window_seconds=60.0,
        )

    message = str(exc.value)
    assert "Full Disk Access" in message
    assert "System Settings" in message
    assert "Magi" in message
    assert str(history_db) in message
