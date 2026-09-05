from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PLUGINS_ROOT = str(Path(__file__).resolve().parents[2])
if PLUGINS_ROOT not in sys.path:
    sys.path.insert(0, PLUGINS_ROOT)

from netease_music.reader import (  # noqa: E402
    NeteaseMusicDatabaseSchemaError,
    NeteaseMusicReader,
)


def _write_history_db(
    db_path: Path,
    *,
    include_playback_tables: bool = True,
    include_playlist_tables: bool = False,
) -> None:
    connection = sqlite3.connect(str(db_path))
    try:
        if include_playback_tables:
            connection.execute(
                """
                CREATE TABLE playingCount (
                    resourceId TEXT,
                    playDuration INTEGER,
                    updateTime INTEGER,
                    source TEXT,
                    resourceType TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE historyTracks (
                    id TEXT,
                    jsonStr TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO playingCount (resourceId, playDuration, updateTime, source, resourceType)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("track-1", 120, 1710000000, "manual", "track"),
            )
            connection.execute(
                "INSERT INTO historyTracks (id, jsonStr) VALUES (?, ?)",
                (
                    "track-1",
                    json.dumps(
                        {
                            "id": "track-1",
                            "name": "Song",
                            "duration": 180000,
                            "artists": [{"id": "artist-1", "name": "Artist"}],
                            "album": {
                                "id": "album-1",
                                "name": "Album",
                                "picUrl": "https://example.com/cover.jpg",
                            },
                        }
                    ),
                ),
            )

        if include_playlist_tables:
            connection.execute("CREATE TABLE web_playlist (pid INTEGER, playlist TEXT)")
            connection.execute("CREATE TABLE web_playlist_track (pid INTEGER, tid TEXT)")
            connection.execute(
                "INSERT INTO web_playlist (pid, playlist) VALUES (?, ?)",
                (1, json.dumps({"specialType": 5})),
            )
            connection.execute(
                "INSERT INTO web_playlist_track (pid, tid) VALUES (?, ?)",
                (1, "track-1"),
            )

        connection.commit()
    finally:
        connection.close()


def test_read_play_records_raises_file_not_found_for_missing_cache_path(tmp_path: Path) -> None:
    reader = NeteaseMusicReader(temp_root=tmp_path / "private-copies")

    with pytest.raises(FileNotFoundError, match="cache database not found"):
        reader.read_play_records(source_path=str(tmp_path / "missing" / "webdb.dat"))


def test_read_play_records_skips_liked_lookup_when_playlist_tables_are_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "webdb.dat"
    _write_history_db(db_path, include_playback_tables=True, include_playlist_tables=False)

    reader = NeteaseMusicReader(temp_root=tmp_path / "private-copies")
    records = reader.read_play_records(source_path=str(db_path))

    assert len(records) == 1
    assert records[0]["track_name"] == "Song"
    assert records[0]["is_liked"] is False


def test_read_play_records_raises_schema_error_when_playback_tables_are_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "webdb.dat"
    _write_history_db(db_path, include_playback_tables=False, include_playlist_tables=False)

    reader = NeteaseMusicReader(temp_root=tmp_path / "private-copies")

    with pytest.raises(NeteaseMusicDatabaseSchemaError, match=r"missing required table\(s\)"):
        reader.read_play_records(source_path=str(db_path))


def test_reader_sweeps_crash_residuals_without_touching_source_or_link_targets(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "webdb.dat"
    _write_history_db(db_path, include_playback_tables=True)
    source_bytes = db_path.read_bytes()
    temp_root = tmp_path / "temporary-database-copies"
    crashed_copy = temp_root / "copy-crashed"
    crashed_copy.mkdir(parents=True)
    (crashed_copy / "database.db").write_bytes(b"private listening history")
    external_file = tmp_path / "external.db"
    external_file.write_bytes(b"must survive")
    (crashed_copy / "external-link").symlink_to(external_file)
    reader = NeteaseMusicReader(temp_root=temp_root)

    records = reader.read_play_records(source_path=str(db_path))

    assert len(records) == 1
    assert db_path.read_bytes() == source_bytes
    assert external_file.read_bytes() == b"must survive"
    assert list(temp_root.iterdir()) == []


def test_reader_replaces_a_symlinked_temp_root_without_following_it(tmp_path: Path) -> None:
    db_path = tmp_path / "webdb.dat"
    _write_history_db(db_path, include_playback_tables=True)
    external_directory = tmp_path / "external"
    external_directory.mkdir()
    external_file = external_directory / "private.db"
    external_file.write_bytes(b"must survive")
    temp_root = tmp_path / "temporary-database-copies"
    temp_root.symlink_to(external_directory, target_is_directory=True)
    reader = NeteaseMusicReader(temp_root=temp_root)

    records = reader.read_play_records(source_path=str(db_path))

    assert len(records) == 1
    assert temp_root.is_dir()
    assert temp_root.is_symlink() is False
    assert list(temp_root.iterdir()) == []
    assert external_file.read_bytes() == b"must survive"
