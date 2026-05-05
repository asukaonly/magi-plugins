"""Timeline sensor for Steam play history."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
from typing import Any

from magi_plugin_sdk.sensors import (
    ContentBlock,
    L2BatchPolicy,
    SensorBase,
    SensorMemoryPolicy,
    SensorOutput,
    SensorOutputMetadata,
    SensorSyncContext,
    SensorSyncResult,
)

from .reader import SteamGameRecord, SteamReader
from .state import SteamPlayStateStore


class SteamPlayHistoryTimelineSensor(SensorBase):
    """Pull-sync sensor backed by local Steam files."""

    sensor_id = "timeline.steam_play_history"
    display_name = "Steam Play History"
    source_type = "steam_play_history"
    memory_event_type = "STEAM_PLAY_SESSION"
    polling_mode = "interval"
    default_interval = 10
    update_key_fields = ("event_kind", "appid", "started_at")
    supports_pull_sync = True
    supports_state_flush = True

    memory_policy = SensorMemoryPolicy(
        memory_domain="external_activity",
        ingest_target="l1_only",
        cognition_eligible=False,
        tom_depth="none",
        retention_class="compressible",
        importance_bias=0.45,
        author_type="external",
        content_type="observation",
    )

    def __init__(
        self,
        *,
        reader: SteamReader | None = None,
        state_store: SteamPlayStateStore | None = None,
        retention_mode: str = "analyze_only",
        steam_path: str = "",
        account_id: str = "auto",
    ) -> None:
        super().__init__()
        self._reader = reader or SteamReader()
        self._state_store = state_store or SteamPlayStateStore()
        self.retention_mode = retention_mode or "analyze_only"
        self.steam_path = steam_path
        self.account_id = account_id or "auto"

    def source_item_identity(self, item: dict[str, Any]) -> str:
        event_kind = str(item.get("event_kind") or "play_session")
        account_hash = str(item.get("account_hash") or "unknown")
        appid = str(item.get("appid") or "unknown")
        if event_kind == "last_played_summary":
            return f"steam:{account_hash}:{appid}:last_played:{int(float(item.get('occurred_at') or 0))}"
        started_ts = _timestamp_from_item(item, "started_at")
        ended_ts = _timestamp_from_item(item, "ended_at")
        return f"steam:{account_hash}:{appid}:session:{int(started_ts)}-{int(ended_ts)}"

    def source_item_version_fingerprint(self, item: dict[str, Any]) -> str:
        return "|".join([
            str(item.get("event_kind") or ""),
            str(item.get("appid") or ""),
            str(item.get("duration_seconds") or 0),
            str(item.get("playtime_forever_minutes_after") or item.get("playtime_forever_minutes") or 0),
        ])

    def l2_batch_policy(self, output: SensorOutput) -> L2BatchPolicy | None:
        ts = output.occurred_at or output.captured_at or time.time()
        try:
            day = time.strftime("%Y%m%d", time.localtime(ts))
        except (OSError, OverflowError, ValueError):
            day = "unknown"
        owner = f"{self.source_type}:{day}"
        return L2BatchPolicy(
            owner=owner,
            catch_up_owner=f"{self.source_type}:catchup",
            max_events=12,
            min_ready_events=4,
            max_wait_seconds=300,
        )

    async def collect_items(self, context: SensorSyncContext) -> SensorSyncResult:
        settings = _sensor_settings(context.plugin_settings)
        now = datetime.now(timezone.utc)
        steam_path = str(settings.get("steam_path") or self.steam_path or "")
        account_id = str(settings.get("account_id") or self.account_id or "auto")
        initial_sync_policy = str(settings.get("initial_sync_policy") or "lookback_days")
        initial_sync_lookback_days = max(1, int(settings.get("initial_sync_lookback_days") or 14))
        max_items = max(1, int(settings.get("max_items_per_sync") or context.limit or 500))

        snapshot = self._reader.read_snapshot(
            steam_path=steam_path,
            account_id=account_id,
        )
        account_hash = snapshot.account.account_hash if snapshot.account else "unknown"
        initial_items: list[dict[str, Any]] = []
        if context.last_cursor is None:
            initial_items = _build_initial_summary_items(
                snapshot.games,
                account_hash=account_hash,
                source=snapshot.source,
                policy=initial_sync_policy,
                lookback_days=initial_sync_lookback_days,
                now=now,
                limit=max_items,
            )

        await self._state_store.apply_snapshot(
            runtime_paths=context.runtime_paths,
            account_hash=account_hash,
            games=snapshot.games,
            now=now,
        )
        session_items = await self._state_store.flush_completed(runtime_paths=context.runtime_paths)
        items = initial_items + session_items
        items = _filter_items(
            items,
            excluded_appids=settings.get("excluded_appids"),
            excluded_keywords=settings.get("excluded_keywords"),
        )[:max_items]

        return SensorSyncResult(
            items=items,
            next_cursor=str(now.timestamp()),
            watermark_ts=now.timestamp(),
            stats={
                "count": len(items),
                "raw_game_count": len(snapshot.games),
                "source": snapshot.source,
                "steam_path_found": bool(snapshot.steam_path),
                "account_found": snapshot.account is not None,
                "initial_sync_policy": initial_sync_policy if context.last_cursor is None else "incremental",
                "errors": snapshot.errors[:3],
            },
        )

    async def flush_runtime_state(self, *, runtime_paths: Any, plugin_settings: dict[str, Any]) -> dict[str, Any]:
        _ = plugin_settings
        return await self._state_store.flush_in_progress(runtime_paths=runtime_paths, now=datetime.now(timezone.utc))

    async def build_output(self, item: dict[str, Any]) -> SensorOutput:
        event_kind = str(item.get("event_kind") or "play_session")
        appid = str(item.get("appid") or "")
        game_name = str(item.get("game_name") or f"Steam app {appid}" if appid else "Steam game")
        account_hash = str(item.get("account_hash") or "unknown")
        source = str(item.get("source") or "local_vdf")
        duration_seconds = max(0, int(item.get("duration_seconds") or 0))
        occurred_at = float(item.get("occurred_at") or _timestamp_from_item(item, "ended_at") or time.time())

        if event_kind == "last_played_summary":
            playtime_minutes = int(item.get("playtime_forever_minutes") or 0)
            headline = self.t(
                "narration.recent_title",
                game_name=game_name,
                fallback=f"Recently played {game_name}",
            )
            summary = self.t(
                "narration.recent_summary",
                game_name=game_name,
                total_playtime=_format_minutes(playtime_minutes * 60),
                fallback=f"Steam reports {game_name} was played recently, with {_format_minutes(playtime_minutes * 60)} total playtime.",
            )
        else:
            headline = self.t(
                "narration.session_title",
                game_name=game_name,
                duration=_format_minutes(duration_seconds),
                fallback=f"Played {game_name} for {_format_minutes(duration_seconds)}",
            )
            summary = self.t(
                "narration.session_summary",
                game_name=game_name,
                duration=_format_minutes(duration_seconds),
                fallback=f"Played {game_name} on Steam for {_format_minutes(duration_seconds)}.",
            )

        blocks = [ContentBlock(kind="text", value=f"Game: {game_name}")]
        if duration_seconds:
            blocks.append(ContentBlock(kind="text", value=f"Duration: {duration_seconds}s"))
        if appid:
            blocks.append(ContentBlock(kind="text", value=f"Steam App ID: {appid}"))
        total_playtime = int(item.get("playtime_forever_minutes_after") or item.get("playtime_forever_minutes") or 0)
        if total_playtime:
            blocks.append(ContentBlock(kind="text", value=f"Total Steam playtime: {_format_minutes(total_playtime * 60)}"))

        return self._build_output(
            source_item_id=self.source_item_identity(item),
            activity=self._build_activity(
                source=self._build_activity_facet(
                    code="steam",
                    i18n_key="activity.source.steam",
                    fallback="Steam",
                    embedding_fallback="Steam",
                ),
                action=self._build_activity_facet(
                    code="play",
                    i18n_key="activity.action.play",
                    fallback="Played",
                    embedding_fallback="玩游戏",
                ),
                object=self._build_activity_facet(
                    code="game",
                    i18n_key="activity.object.game",
                    fallback="Game",
                    embedding_fallback="游戏",
                ),
            ),
            narration=self._build_narration(title=headline, body=summary),
            occurred_at=occurred_at,
            content_blocks=blocks,
            tags=_tags(appid=appid, game_name=game_name),
            provenance={
                "sensor_id": self.sensor_id,
                "event_kind": event_kind,
                "source": source,
                "account_hash": account_hash,
                "appid": appid,
                "game_name": game_name,
                "started_at": str(item.get("started_at") or ""),
                "ended_at": str(item.get("ended_at") or ""),
                "duration_seconds": duration_seconds,
                "playtime_forever_minutes_before": int(item.get("playtime_forever_minutes_before") or 0),
                "playtime_forever_minutes_after": int(item.get("playtime_forever_minutes_after") or total_playtime),
                "playtime_forever_minutes": total_playtime,
                "playtime_two_weeks_minutes": int(item.get("playtime_two_weeks_minutes") or 0),
                "last_played_ts": float(item.get("last_played_ts") or 0.0),
                "installed": bool(item.get("installed")),
                "confidence": str(item.get("confidence") or ""),
            },
            domain_payload={
                "retention_mode": self.retention_mode,
                "event_kind": event_kind,
                "appid": appid,
                "game_name": game_name,
                "duration_seconds": duration_seconds,
                "playtime_forever_minutes": total_playtime,
            },
        )

    async def extract_metadata(self, item: dict[str, Any]) -> SensorOutputMetadata:
        appid = str(item.get("appid") or "")
        game_name = str(item.get("game_name") or "")
        return SensorOutputMetadata(
            entities=[{"type": "game", "name": game_name, "id": appid}] if game_name else [],
            tags=_tags(appid=appid, game_name=game_name),
            relation_candidates=[],
        )


def _sensor_settings(plugin_settings: dict[str, Any]) -> dict[str, Any]:
    sensors = plugin_settings.get("sensors", {}) if isinstance(plugin_settings, dict) else {}
    if not isinstance(sensors, dict):
        return {}
    value = sensors.get("steam_play_history", {})
    return value if isinstance(value, dict) else {}


def _build_initial_summary_items(
    games: list[SteamGameRecord],
    *,
    account_hash: str,
    source: str,
    policy: str,
    lookback_days: int,
    now: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    if policy == "from_now":
        return []
    cutoff = now - timedelta(days=lookback_days)
    items: list[dict[str, Any]] = []
    for game in games:
        if not game.last_played_ts or game.playtime_forever_minutes <= 0:
            continue
        occurred_at = datetime.fromtimestamp(game.last_played_ts, tz=timezone.utc)
        if policy != "full" and occurred_at < cutoff:
            continue
        items.append({
            "event_kind": "last_played_summary",
            "account_hash": account_hash,
            "appid": game.appid,
            "game_name": game.name,
            "source": source,
            "occurred_at": float(game.last_played_ts),
            "last_played_ts": float(game.last_played_ts),
            "playtime_forever_minutes": int(game.playtime_forever_minutes),
            "playtime_two_weeks_minutes": int(game.playtime_two_weeks_minutes),
            "installed": bool(game.installed),
            "confidence": "steam_last_played_summary",
        })
        if len(items) >= limit:
            break
    return items


def _filter_items(items: list[dict[str, Any]], *, excluded_appids: Any, excluded_keywords: Any) -> list[dict[str, Any]]:
    appids = {str(value).strip() for value in excluded_appids or [] if str(value).strip()}
    keywords = [str(value).strip().lower() for value in excluded_keywords or [] if str(value).strip()]
    if not appids and not keywords:
        return items
    kept: list[dict[str, Any]] = []
    for item in items:
        appid = str(item.get("appid") or "").strip()
        game_name = str(item.get("game_name") or "").lower()
        if appid in appids:
            continue
        if any(keyword in game_name for keyword in keywords):
            continue
        kept.append(item)
    return kept


def _tags(*, appid: str, game_name: str) -> list[str]:
    tags = ["steam", "gaming"]
    if appid:
        tags.append(f"steam_app:{appid}")
    normalized_name = "_".join(part for part in game_name.lower().split() if part)
    if normalized_name:
        tags.append(f"game:{normalized_name[:64]}")
    return tags


def _format_minutes(seconds: int) -> str:
    minutes = max(0, round(seconds / 60))
    if minutes >= 60:
        hours = minutes // 60
        remainder = minutes % 60
        return f"{hours}h {remainder}m" if remainder else f"{hours}h"
    return f"{minutes}m"


def _timestamp_from_item(item: dict[str, Any], key: str) -> float:
    raw = item.get(key)
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()
