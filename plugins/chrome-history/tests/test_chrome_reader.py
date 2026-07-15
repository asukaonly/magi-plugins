from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType


def _load_plugin_module(module_name: str) -> ModuleType:
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "chrome_history_reader_under_test"
    if package_name not in sys.modules:
        package = ModuleType(package_name)
        package.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
        sys.modules[package_name] = package
    qualified_name = f"{package_name}.{module_name}"
    if qualified_name in sys.modules:
        return sys.modules[qualified_name]
    spec = importlib.util.spec_from_file_location(qualified_name, plugin_dir / f"{module_name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified_name] = module
    spec.loader.exec_module(module)
    return module


def _build_history_database(root: Path) -> None:
    profile_dir = root / "Default"
    profile_dir.mkdir(parents=True)
    database_path = profile_dir / "History"
    normalizers = _load_plugin_module("normalizers")
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER)"
        )
        connection.execute(
            "CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER, from_visit INTEGER, transition INTEGER)"
        )
        visit_dates = [
            datetime(2026, 5, 31, 23, 59),
            datetime(2026, 6, 1, 0, 0),
            datetime(2026, 6, 30, 23, 59),
            datetime(2026, 7, 1, 0, 0),
        ]
        for index, visited_at in enumerate(visit_dates, start=1):
            connection.execute(
                "INSERT INTO urls (id, url, title, visit_count) VALUES (?, ?, ?, ?)",
                (index, f"https://example{index}.com/", f"Page {index}", 1),
            )
            connection.execute(
                "INSERT INTO visits (id, url, visit_time, from_visit, transition) VALUES (?, ?, ?, 0, ?)",
                (
                    index,
                    index,
                    normalizers.unix_seconds_to_chrome_time(visited_at.timestamp()),
                    0x20000000,
                ),
            )
        connection.commit()
    finally:
        connection.close()


def test_custom_date_range_is_inclusive_and_stays_bounded_on_continuation(tmp_path: Path) -> None:
    _build_history_database(tmp_path)
    reader_module = _load_plugin_module("chrome_reader")
    reader = reader_module.ChromeHistoryReader()
    start_at = datetime(2026, 6, 1).timestamp()
    end_at = datetime(2026, 7, 1).timestamp()

    initial = reader.read_visits(
        source_path=str(tmp_path),
        profile="Default",
        limit=10,
        last_cursor=None,
        initial_lookback_hours=None,
        initial_start_time=start_at,
        initial_end_time=end_at,
        merge_window_seconds=0,
    )
    continued = reader.read_visits(
        source_path=str(tmp_path),
        profile="Default",
        limit=10,
        last_cursor="2",
        initial_lookback_hours=None,
        initial_start_time=start_at,
        initial_end_time=end_at,
        merge_window_seconds=0,
    )

    assert [item["visit_id"] for item in initial] == ["2", "3"]
    assert [item["visit_id"] for item in continued] == ["3"]
