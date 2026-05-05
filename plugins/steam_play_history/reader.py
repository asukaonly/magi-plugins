"""Readers for local Steam play history and optional Steam Web API data."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

logger = logging.getLogger(__name__)

STEAM_ID64_OFFSET = 76561197960265728


@dataclass(slots=True)
class SteamAccount:
    """Steam account selected for play-history ingestion."""

    account_id: str
    steamid64: str
    persona_name: str = ""
    account_name: str = ""
    most_recent: bool = False

    @property
    def account_hash(self) -> str:
        value = self.steamid64 or self.account_id or self.account_name or "unknown"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class SteamGameRecord:
    """Normalized Steam game state for one poll."""

    appid: str
    name: str
    playtime_forever_minutes: int = 0
    playtime_two_weeks_minutes: int = 0
    last_played_ts: float | None = None
    installed: bool = False
    library_path: str = ""
    install_dir: str = ""
    size_on_disk: int = 0
    source: str = "local_vdf"
    icon_url: str = ""


@dataclass(slots=True)
class SteamSnapshot:
    """Steam state read from one or more sources."""

    account: SteamAccount | None
    games: list[SteamGameRecord] = field(default_factory=list)
    steam_path: str = ""
    source: str = "local_vdf"
    errors: list[str] = field(default_factory=list)


class SteamReader:
    """Read Steam playtime data from local Steam files and optional Web API."""

    def read_snapshot(
        self,
        *,
        steam_path: str | None = None,
        account_id: str | None = None,
        source_mode: str = "local",
        steamid64: str | None = None,
        web_api_key: str | None = None,
        include_uninstalled_games: bool = False,
    ) -> SteamSnapshot:
        mode = _normalize_source_mode(source_mode)
        errors: list[str] = []

        local_snapshot = self._read_local_snapshot(
            steam_path=steam_path,
            account_id=account_id,
            include_uninstalled_games=include_uninstalled_games,
        )
        errors.extend(local_snapshot.errors)

        resolved_steamid64 = steamid64 or (local_snapshot.account.steamid64 if local_snapshot.account else "")
        if mode == "local" or not web_api_key or not resolved_steamid64:
            return local_snapshot

        api_snapshot = self._read_api_snapshot(
            steamid64=resolved_steamid64,
            web_api_key=web_api_key,
        )
        errors.extend(api_snapshot.errors)
        if mode == "web_api":
            api_snapshot.errors = errors
            return api_snapshot

        merged = _merge_snapshots(local_snapshot, api_snapshot)
        merged.errors = errors
        return merged

    def _read_local_snapshot(
        self,
        *,
        steam_path: str | None,
        account_id: str | None,
        include_uninstalled_games: bool,
    ) -> SteamSnapshot:
        root = _resolve_steam_root(steam_path)
        if root is None:
            return SteamSnapshot(account=None, errors=["Steam installation was not found."])

        accounts = _read_login_users(root)
        account = _select_account(accounts, account_id)
        libraries = _read_library_folders(root)
        manifest_games = _read_app_manifests(libraries)

        playtime_by_app: dict[str, dict[str, Any]] = {}
        if account is not None and account.account_id:
            playtime_by_app = _read_localconfig_apps(root, account.account_id)

        games_by_appid: dict[str, SteamGameRecord] = dict(manifest_games)
        for appid, values in playtime_by_app.items():
            if appid in games_by_appid:
                game = games_by_appid[appid]
            elif include_uninstalled_games:
                game = SteamGameRecord(appid=appid, name=f"Steam app {appid}", source="local_vdf")
                games_by_appid[appid] = game
            else:
                continue

            game.playtime_forever_minutes = max(
                game.playtime_forever_minutes,
                _int_value(values.get("playtime_forever_minutes")),
            )
            game.playtime_two_weeks_minutes = max(
                game.playtime_two_weeks_minutes,
                _int_value(values.get("playtime_two_weeks_minutes")),
            )
            last_played = _float_or_none(values.get("last_played_ts"))
            if last_played:
                game.last_played_ts = last_played

        games = sorted(games_by_appid.values(), key=_game_sort_key)
        return SteamSnapshot(
            account=account,
            games=games,
            steam_path=str(root),
            source="local_vdf",
        )

    def _read_api_snapshot(self, *, steamid64: str, web_api_key: str) -> SteamSnapshot:
        params = urlencode({
            "key": web_api_key,
            "steamid": steamid64,
            "format": "json",
            "include_appinfo": "true",
            "include_played_free_games": "true",
        })
        url = f"https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/?{params}"
        try:
            with urlopen(url, timeout=10) as response:  # noqa: S310 - user-supplied Steam API endpoint only.
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # pragma: no cover - depends on network and user credentials.
            logger.warning("Failed to read Steam Web API data: %s", exc)
            return SteamSnapshot(
                account=_account_from_steamid64(steamid64),
                source="steam_web_api",
                errors=["Steam Web API request failed."],
            )

        response = payload.get("response") if isinstance(payload, dict) else {}
        raw_games = response.get("games") if isinstance(response, dict) else []
        games: list[SteamGameRecord] = []
        if isinstance(raw_games, list):
            for raw in raw_games:
                if not isinstance(raw, dict):
                    continue
                appid = str(raw.get("appid") or "").strip()
                if not appid:
                    continue
                icon_hash = str(raw.get("img_icon_url") or "").strip()
                games.append(
                    SteamGameRecord(
                        appid=appid,
                        name=str(raw.get("name") or f"Steam app {appid}"),
                        playtime_forever_minutes=_int_value(raw.get("playtime_forever")),
                        playtime_two_weeks_minutes=_int_value(raw.get("playtime_2weeks")),
                        last_played_ts=_float_or_none(raw.get("rtime_last_played")),
                        installed=False,
                        source="steam_web_api",
                        icon_url=(
                            f"https://media.steampowered.com/steamcommunity/public/images/apps/{appid}/{icon_hash}.jpg"
                            if icon_hash else ""
                        ),
                    )
                )

        return SteamSnapshot(
            account=_account_from_steamid64(steamid64),
            games=sorted(games, key=_game_sort_key),
            source="steam_web_api",
        )


def _normalize_source_mode(value: str | None) -> str:
    mode = str(value or "local").strip().lower()
    return mode if mode in {"local", "hybrid", "web_api"} else "local"


def _resolve_steam_root(steam_path: str | None) -> Path | None:
    if steam_path:
        path = Path(os.path.expanduser(steam_path)).resolve()
        if _looks_like_steam_root(path):
            return path

    for candidate in _candidate_steam_roots():
        if _looks_like_steam_root(candidate):
            return candidate
    return None


def _looks_like_steam_root(path: Path) -> bool:
    return (path / "config" / "loginusers.vdf").exists() or (path / "steamapps").exists()


def _candidate_steam_roots() -> list[Path]:
    candidates: list[Path] = []
    if sys.platform == "win32":
        candidates.extend(_windows_steam_roots())
        for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
            value = os.environ.get(env_name)
            if value:
                candidates.append(Path(value) / "Steam")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "Steam")
    else:
        candidates.extend([
            Path.home() / ".steam" / "steam",
            Path.home() / ".local" / "share" / "Steam",
        ])
    return _dedupe_paths(candidates)


def _windows_steam_roots() -> list[Path]:
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows-only module.
        return []

    roots: list[Path] = []
    keys = [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ]
    for hive, key_path, value_name in keys:
        try:
            with winreg.OpenKey(hive, key_path) as key:
                raw, _ = winreg.QueryValueEx(key, value_name)
        except OSError:
            continue
        if raw:
            roots.append(Path(str(raw)))
    return roots


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path).replace("\\", "/").rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _read_login_users(root: Path) -> list[SteamAccount]:
    path = root / "config" / "loginusers.vdf"
    data = _read_vdf_file(path)
    users = data.get("users") if isinstance(data, dict) else {}
    if not isinstance(users, dict):
        return []

    accounts: list[SteamAccount] = []
    for steamid64, values in users.items():
        if not isinstance(values, dict):
            continue
        steamid64_text = str(steamid64)
        accounts.append(
            SteamAccount(
                account_id=_account_id_from_steamid64(steamid64_text),
                steamid64=steamid64_text,
                persona_name=str(values.get("PersonaName") or ""),
                account_name=str(values.get("AccountName") or ""),
                most_recent=str(values.get("MostRecent") or "0") == "1",
            )
        )
    return accounts


def _select_account(accounts: list[SteamAccount], account_id: str | None) -> SteamAccount | None:
    requested = str(account_id or "").strip().lower()
    if requested and requested != "auto":
        for account in accounts:
            choices = {
                account.account_id.lower(),
                account.steamid64.lower(),
                account.account_name.lower(),
                account.persona_name.lower(),
            }
            if requested in choices:
                return account
    for account in accounts:
        if account.most_recent:
            return account
    return accounts[0] if accounts else None


def _account_from_steamid64(steamid64: str) -> SteamAccount:
    return SteamAccount(
        account_id=_account_id_from_steamid64(steamid64),
        steamid64=str(steamid64),
    )


def _account_id_from_steamid64(steamid64: str) -> str:
    try:
        value = int(steamid64)
    except (TypeError, ValueError):
        return ""
    if value < STEAM_ID64_OFFSET:
        return str(value)
    return str(value - STEAM_ID64_OFFSET)


def _read_library_folders(root: Path) -> list[Path]:
    libraries = [root]
    data = _read_vdf_file(root / "steamapps" / "libraryfolders.vdf")
    folders = data.get("libraryfolders") if isinstance(data, dict) else {}
    if isinstance(folders, dict):
        for value in folders.values():
            if isinstance(value, dict):
                raw_path = value.get("path")
            else:
                raw_path = value
            if raw_path:
                libraries.append(Path(str(raw_path)))
    return _dedupe_paths(libraries)


def _read_app_manifests(library_roots: list[Path]) -> dict[str, SteamGameRecord]:
    games: dict[str, SteamGameRecord] = {}
    for library_root in library_roots:
        steamapps = library_root / "steamapps"
        if not steamapps.exists():
            continue
        for manifest in steamapps.glob("appmanifest_*.acf"):
            data = _read_vdf_file(manifest)
            app_state = data.get("AppState") if isinstance(data, dict) else {}
            if not isinstance(app_state, dict):
                continue
            appid = str(app_state.get("appid") or manifest.stem.removeprefix("appmanifest_")).strip()
            if not appid:
                continue
            games[appid] = SteamGameRecord(
                appid=appid,
                name=str(app_state.get("name") or f"Steam app {appid}"),
                last_played_ts=_float_or_none(app_state.get("LastPlayed")),
                installed=True,
                library_path=str(library_root),
                install_dir=str(app_state.get("installdir") or ""),
                size_on_disk=_int_value(app_state.get("SizeOnDisk")),
                source="local_vdf",
            )
    return games


def _read_localconfig_apps(root: Path, account_id: str) -> dict[str, dict[str, Any]]:
    data = _read_vdf_file(root / "userdata" / account_id / "config" / "localconfig.vdf")
    store = data.get("UserLocalConfigStore") if isinstance(data, dict) else {}
    apps = _dig(store, "Software", "Valve", "Steam", "apps")
    if not isinstance(apps, dict):
        return {}

    result: dict[str, dict[str, Any]] = {}
    for appid, values in apps.items():
        if not isinstance(values, dict):
            continue
        result[str(appid)] = {
            "playtime_forever_minutes": _first_int(
                values,
                ("Playtime", "playtime", "playtime_forever", "PlaytimeForever"),
            ),
            "playtime_two_weeks_minutes": _first_int(
                values,
                ("Playtime2wks", "playtime_2weeks", "playtime_two_weeks"),
            ),
            "last_played_ts": _first_int(values, ("LastPlayed", "last_played", "rtime_last_played")),
        }
    return result


def _read_vdf_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return parse_vdf(path.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception as exc:
        logger.warning("Failed to parse Steam VDF file %s: %s", path, exc)
        return {}


def parse_vdf(text: str) -> dict[str, Any]:
    """Parse Steam KeyValues text into nested dictionaries."""
    tokens = list(_tokenize_vdf(text))
    index = 0

    def parse_object(stop_at_brace: bool = False) -> dict[str, Any]:
        nonlocal index
        result: dict[str, Any] = {}
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if token == "}":
                if stop_at_brace:
                    break
                continue
            if token == "{":
                continue
            if index < len(tokens) and tokens[index] == "{":
                index += 1
                result[token] = parse_object(stop_at_brace=True)
            elif index < len(tokens):
                result[token] = tokens[index]
                index += 1
            else:
                result[token] = ""
        return result

    return parse_object()


def _tokenize_vdf(text: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            newline = text.find("\n", index)
            index = length if newline == -1 else newline + 1
            continue
        if char in "{}":
            tokens.append(char)
            index += 1
            continue
        if char == '"':
            index += 1
            value_chars: list[str] = []
            while index < length:
                current = text[index]
                if current == "\\" and index + 1 < length:
                    index += 1
                    value_chars.append(text[index])
                    index += 1
                    continue
                if current == '"':
                    index += 1
                    break
                value_chars.append(current)
                index += 1
            tokens.append("".join(value_chars))
            continue
        start = index
        while index < length and not text[index].isspace() and text[index] not in "{}":
            index += 1
        tokens.append(text[start:index])
    return tokens


def _dig(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_int(values: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        result = _int_value(values.get(key), default=-1)
        if result >= 0:
            return result
    return 0


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return int(default)


def _float_or_none(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _game_sort_key(game: SteamGameRecord) -> tuple[float, int, str]:
    return (-(game.last_played_ts or 0.0), -game.playtime_forever_minutes, game.name.lower())


def _merge_snapshots(local: SteamSnapshot, api: SteamSnapshot) -> SteamSnapshot:
    games: dict[str, SteamGameRecord] = {game.appid: game for game in local.games}
    for api_game in api.games:
        local_game = games.get(api_game.appid)
        if local_game is None:
            games[api_game.appid] = api_game
            continue
        local_game.name = api_game.name or local_game.name
        local_game.playtime_forever_minutes = max(
            local_game.playtime_forever_minutes,
            api_game.playtime_forever_minutes,
        )
        local_game.playtime_two_weeks_minutes = max(
            local_game.playtime_two_weeks_minutes,
            api_game.playtime_two_weeks_minutes,
        )
        if api_game.last_played_ts:
            local_game.last_played_ts = api_game.last_played_ts
        local_game.icon_url = api_game.icon_url or local_game.icon_url
        local_game.source = "hybrid"

    return SteamSnapshot(
        account=local.account or api.account,
        games=sorted(games.values(), key=_game_sort_key),
        steam_path=local.steam_path,
        source="hybrid",
    )
