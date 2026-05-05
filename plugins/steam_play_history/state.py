"""Persistent state for Steam play-session inference."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .reader import SteamGameRecord

DEFAULT_IDLE_TIMEOUT_S = 15 * 60
DEFAULT_MIN_SESSION_S = 3 * 60


class SteamPlayStateStore:
    """Track playtime deltas and flush completed Steam sessions."""

    _locks: dict[str, asyncio.Lock] = {}

    def __init__(self, *, idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S, min_session_s: int = DEFAULT_MIN_SESSION_S) -> None:
        self._idle_timeout_s = max(60, int(idle_timeout_s))
        self._min_session_s = max(0, int(min_session_s))

    def _state_path(self, runtime_paths: Any) -> Path:
        return runtime_paths.plugin_cache_dir("steam_play_history") / "state.json"

    def _lock_for(self, path: Path) -> asyncio.Lock:
        key = str(path.resolve())
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _load(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"accounts": {}, "current_sessions": {}, "completed": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"accounts": {}, "current_sessions": {}, "completed": []}
        if not isinstance(data, dict):
            return {"accounts": {}, "current_sessions": {}, "completed": []}
        data.setdefault("accounts", {})
        data.setdefault("current_sessions", {})
        data.setdefault("completed", [])
        return data

    def _save(self, path: Path, state: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=True, sort_keys=True), encoding="utf-8")

    async def apply_snapshot(
        self,
        *,
        runtime_paths: Any,
        account_hash: str,
        games: list[SteamGameRecord],
        now: datetime,
    ) -> None:
        """Update in-flight sessions from a Steam playtime snapshot."""
        path = self._state_path(runtime_paths)
        async with self._lock_for(path):
            state = self._load(path)
            accounts = state.setdefault("accounts", {})
            account_state = accounts.setdefault(account_hash, {"games": {}})
            known_games = account_state.setdefault("games", {})
            current_sessions = state.setdefault("current_sessions", {})
            completed: list[dict[str, Any]] = list(state.get("completed") or [])
            now_iso = now.isoformat()
            updated_session_keys: set[str] = set()

            for game in games:
                appid = str(game.appid)
                previous = known_games.get(appid) if isinstance(known_games.get(appid), dict) else None
                previous_minutes = _int_value(previous.get("playtime_forever_minutes") if previous else 0)
                current_minutes = max(0, int(game.playtime_forever_minutes))
                delta_minutes = max(0, current_minutes - previous_minutes)

                if previous is not None and delta_minutes > 0:
                    session_key = f"{account_hash}:{appid}"
                    session = current_sessions.get(session_key)
                    if not isinstance(session, dict):
                        started_at = now - timedelta(minutes=delta_minutes)
                        session = {
                            "account_hash": account_hash,
                            "appid": appid,
                            "game_name": game.name,
                            "source": game.source,
                            "started_at": started_at.isoformat(),
                            "last_seen_at": now_iso,
                            "duration_seconds": 0,
                            "playtime_forever_minutes_before": previous_minutes,
                            "playtime_forever_minutes_after": previous_minutes,
                            "installed": bool(game.installed),
                        }
                    session["game_name"] = game.name
                    session["source"] = game.source
                    session["last_seen_at"] = now_iso
                    session["duration_seconds"] = _int_value(session.get("duration_seconds")) + delta_minutes * 60
                    session["playtime_forever_minutes_after"] = current_minutes
                    session["installed"] = bool(game.installed)
                    current_sessions[session_key] = session
                    updated_session_keys.add(session_key)

                known_games[appid] = {
                    "name": game.name,
                    "playtime_forever_minutes": current_minutes,
                    "playtime_two_weeks_minutes": int(game.playtime_two_weeks_minutes),
                    "last_played_ts": game.last_played_ts,
                    "last_seen_at": now_iso,
                    "installed": bool(game.installed),
                    "source": game.source,
                }

            for session_key, session in list(current_sessions.items()):
                if not isinstance(session, dict):
                    current_sessions.pop(session_key, None)
                    continue
                if str(session.get("account_hash") or "") != account_hash:
                    continue
                if session_key in updated_session_keys:
                    continue
                last_seen = _parse_datetime(session.get("last_seen_at"))
                if (now - last_seen).total_seconds() >= self._idle_timeout_s:
                    self._close_session(session, completed)
                    current_sessions.pop(session_key, None)

            state["completed"] = completed
            self._save(path, state)

    def _close_session(self, session: dict[str, Any], completed: list[dict[str, Any]]) -> None:
        duration_seconds = _int_value(session.get("duration_seconds"))
        if duration_seconds < self._min_session_s:
            return
        completed.append({
            "event_kind": "play_session",
            "account_hash": str(session.get("account_hash") or ""),
            "appid": str(session.get("appid") or ""),
            "game_name": str(session.get("game_name") or ""),
            "source": str(session.get("source") or "local_vdf"),
            "started_at": str(session.get("started_at") or ""),
            "ended_at": str(session.get("last_seen_at") or ""),
            "duration_seconds": duration_seconds,
            "playtime_forever_minutes_before": _int_value(session.get("playtime_forever_minutes_before")),
            "playtime_forever_minutes_after": _int_value(session.get("playtime_forever_minutes_after")),
            "installed": bool(session.get("installed")),
            "confidence": "inferred_from_playtime_delta",
        })

    async def flush_completed(self, *, runtime_paths: Any) -> list[dict[str, Any]]:
        """Return and clear completed Steam play sessions."""
        path = self._state_path(runtime_paths)
        async with self._lock_for(path):
            state = self._load(path)
            completed = list(state.get("completed") or [])
            state["completed"] = []
            self._save(path, state)
        return completed

    async def flush_in_progress(self, *, runtime_paths: Any, now: datetime) -> dict[str, Any]:
        """Return diagnostics about currently inferred Steam sessions."""
        path = self._state_path(runtime_paths)
        async with self._lock_for(path):
            state = self._load(path)
            sessions = [session for session in state.get("current_sessions", {}).values() if isinstance(session, dict)]
            pending = len(state.get("completed") or [])
        return {
            "current_games": [str(session.get("game_name") or session.get("appid") or "") for session in sessions],
            "pending_sessions": pending,
            "checked_at": now.isoformat(),
        }


def _parse_datetime(value: Any) -> datetime:
    text = str(value or "").strip()
    if text:
        try:
            result = datetime.fromisoformat(text)
            if result.tzinfo is None:
                return result.replace(tzinfo=timezone.utc)
            return result
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return int(default)
