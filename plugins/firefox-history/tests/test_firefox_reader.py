from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


def _load_firefox_reader():
    module_path = Path(__file__).resolve().parents[1] / "firefox_reader.py"
    spec = importlib.util.spec_from_file_location("firefox_history_reader", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_places_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE moz_places (
                id INTEGER PRIMARY KEY,
                url LONGVARCHAR,
                title LONGVARCHAR,
                visit_count INTEGER
            );
            CREATE TABLE moz_historyvisits (
                id INTEGER PRIMARY KEY,
                place_id INTEGER,
                visit_date INTEGER,
                from_visit INTEGER,
                visit_type INTEGER
            );
            """
        )
        connection.execute(
            "INSERT INTO moz_places (id, url, title, visit_count) VALUES (1, ?, ?, 2)",
            ("https://mozilla.org/products", "Mozilla Products"),
        )
        connection.execute(
            "INSERT INTO moz_historyvisits (id, place_id, visit_date, from_visit, visit_type) VALUES (?, ?, ?, ?, ?)",
            (1, 1, 1_700_000_000 * 1_000_000, 0, 1),
        )
        connection.execute(
            "INSERT INTO moz_historyvisits (id, place_id, visit_date, from_visit, visit_type) VALUES (?, ?, ?, ?, ?)",
            (2, 1, 1_700_000_100 * 1_000_000, 0, 4),
        )
        connection.commit()
    finally:
        connection.close()


def test_firefox_reader_auto_detects_default_profile_and_filters_embed(tmp_path: Path) -> None:
    reader_module = _load_firefox_reader()
    reader = reader_module.FirefoxHistoryReader()

    profile_dir = tmp_path / "Profiles" / "abcd.default-release"
    profile_dir.mkdir(parents=True, exist_ok=True)
    _write_places_db(profile_dir / "places.sqlite")

    (tmp_path / "profiles.ini").write_text(
        "\n".join(
            [
                "[Profile0]",
                "Name=default-release",
                "IsRelative=1",
                "Path=Profiles/abcd.default-release",
                "Default=1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    items = reader.read_visits(
        source_path=str(tmp_path),
        profile="",
        limit=20,
        initial_lookback_hours=None,
        merge_window_seconds=60.0,
    )

    assert len(items) == 1
    assert items[0]["title"] == "Mozilla Products"
    assert items[0]["domain"] == "mozilla.org"
    assert abs(float(items[0]["visit_time"]) - 1_700_000_000) < 1
    assert reader.get_latest_visit_id(source_path=str(tmp_path), profile="") == 2
