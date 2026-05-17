from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


def _load_edge_reader():
    module_path = Path(__file__).resolve().parents[1] / "edge_reader.py"
    spec = importlib.util.spec_from_file_location("edge_history_reader", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _to_chromium_microseconds(unix_seconds: int) -> int:
    return int((unix_seconds + 11644473600) * 1_000_000)


def _write_history_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE urls (
                id INTEGER PRIMARY KEY,
                url LONGVARCHAR,
                title LONGVARCHAR,
                visit_count INTEGER
            );
            CREATE TABLE visits (
                id INTEGER PRIMARY KEY,
                url INTEGER,
                visit_time INTEGER,
                from_visit INTEGER,
                transition INTEGER
            );
            """
        )
        connection.execute(
            "INSERT INTO urls (id, url, title, visit_count) VALUES (1, ?, ?, 2)",
            ("https://example.com/docs", "Example Docs"),
        )
        connection.execute(
            "INSERT INTO visits (id, url, visit_time, from_visit, transition) VALUES (?, ?, ?, ?, ?)",
            (1, 1, _to_chromium_microseconds(1_700_000_000), 0, 0x20000000),
        )
        connection.execute(
            "INSERT INTO visits (id, url, visit_time, from_visit, transition) VALUES (?, ?, ?, ?, ?)",
            (2, 1, _to_chromium_microseconds(1_700_000_100), 0, 0),
        )
        connection.commit()
    finally:
        connection.close()


def test_edge_reader_filters_non_chain_end_visits(tmp_path: Path) -> None:
    reader_module = _load_edge_reader()
    reader = reader_module.EdgeHistoryReader()

    profile_dir = tmp_path / "Default"
    profile_dir.mkdir(parents=True, exist_ok=True)
    _write_history_db(profile_dir / "History")

    items = reader.read_visits(
        source_path=str(tmp_path),
        profile="Default",
        limit=20,
        initial_lookback_hours=None,
        merge_window_seconds=60.0,
    )

    assert len(items) == 1
    assert items[0]["title"] == "Example Docs"
    assert items[0]["domain"] == "example.com"
    assert abs(float(items[0]["visit_time"]) - 1_700_000_000) < 1
    assert reader.get_latest_visit_id(source_path=str(tmp_path), profile="Default") == 2
