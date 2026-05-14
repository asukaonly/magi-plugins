from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PLUGINS_ROOT = str(Path(__file__).resolve().parents[2])
if PLUGINS_ROOT not in sys.path:
    sys.path.insert(0, PLUGINS_ROOT)

from netease_music.reader import NeteaseMusicDatabaseSchemaError, NeteaseMusicReader


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
    reader = NeteaseMusicReader()

    with pytest.raises(FileNotFoundError, match="cache database not found"):
        reader.read_play_records(source_path=str(tmp_path / "missing" / "webdb.dat"))


def test_read_play_records_skips_liked_lookup_when_playlist_tables_are_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "webdb.dat"
    _write_history_db(db_path, include_playback_tables=True, include_playlist_tables=False)

    reader = NeteaseMusicReader()
    records = reader.read_play_records(source_path=str(db_path))

    assert len(records) == 1
    assert records[0]["track_name"] == "Song"
    assert records[0]["is_liked"] is False


def test_read_play_records_raises_schema_error_when_playback_tables_are_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "webdb.dat"
    _write_history_db(db_path, include_playback_tables=False, include_playlist_tables=False)

    reader = NeteaseMusicReader()

    with pytest.raises(NeteaseMusicDatabaseSchemaError, match=r"missing required table\(s\)"):
        reader.read_play_records(source_path=str(db_path))