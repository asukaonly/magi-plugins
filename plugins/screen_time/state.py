"""Shared state store for foreground-app usage aggregation.

The store maintains two pieces of state per plugin cache:

- ``last_activation``: the most recent foreground app observation, used as the
  open endpoint when computing how much time the current session has consumed.
- ``open_buckets``: per-hour usage buckets keyed by ``bucket_start::canonical_id``
  that accumulate until they slip past the current hour and are flushed by the
  sensor.

Both the in-plugin Windows / macOS watcher and any future external producer
funnel activations through :meth:`ScreenTimeStateStore.apply_activation`, so
the bucket layout is identical regardless of where samples originated.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class ScreenTimeStateStore:
    """Persist open app-usage buckets and the current active session."""

    _locks: dict[str, asyncio.Lock] = {}

    def _state_path(self, runtime_paths: Any) -> Path:
        return runtime_paths.plugin_cache_dir("screen_time") / "state.json"

    def _lock_for(self, path: Path) -> asyncio.Lock:
        key = str(path.resolve())
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _load_state(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"last_activation": None, "open_buckets": {}}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"last_activation": None, "open_buckets": {}}

    def _save_state(self, path: Path, state: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=True, sort_keys=True), encoding="utf-8")

    def _floor_hour(self, value: datetime) -> datetime:
        return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)

    def _increment_buckets(
        self,
        open_buckets: dict[str, dict[str, Any]],
        *,
        session_id: str,
        bundle_id: str,
        app_name: str,
        canonical_id: str,
        display_name: str,
        platform: str,
        start_at: datetime,
        end_at: datetime,
    ) -> None:
        cursor = start_at
        while cursor < end_at:
            bucket_start = self._floor_hour(cursor)
            bucket_end = bucket_start + timedelta(hours=1)
            segment_end = min(bucket_end, end_at)
            duration = max(0, int((segment_end - cursor).total_seconds()))
            if duration <= 0:
                break

            key = f"{bucket_start.isoformat()}::{canonical_id}"
            bucket = open_buckets.setdefault(
                key,
                {
                    "bucket_start": bucket_start.isoformat(),
                    "bucket_end": bucket_end.isoformat(),
                    "bundle_id": bundle_id,
                    "app_name": app_name,
                    "canonical_id": canonical_id,
                    "display_name": display_name,
                    "platform": platform,
                    "duration_seconds": 0,
                    "session_count": 0,
                    "last_session_id": None,
                },
            )
            bucket["duration_seconds"] += duration
            # Keep the latest display name / platform we observed — they may
            # change over time as the catalog or OS metadata is updated.
            bucket["bundle_id"] = bundle_id or bucket.get("bundle_id", "")
            bucket["app_name"] = app_name or bucket.get("app_name", "")
            bucket["display_name"] = display_name or bucket.get("display_name", "")
            bucket["platform"] = platform or bucket.get("platform", "")
            if bucket.get("last_session_id") != session_id:
                bucket["session_count"] += 1
                bucket["last_session_id"] = session_id
            cursor = segment_end

    def _carry_forward_last_activation(
        self,
        *,
        open_buckets: dict[str, dict[str, Any]],
        last_activation: dict[str, Any],
        end_at: datetime,
    ) -> None:
        observed_at = last_activation.get("observed_at")
        canonical_id = str(last_activation.get("canonical_id") or "")
        if not observed_at or not canonical_id:
            return
        start_at = datetime.fromisoformat(str(observed_at))
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=timezone.utc)
        if end_at <= start_at:
            return
        self._increment_buckets(
            open_buckets,
            session_id=str(last_activation.get("session_id") or ""),
            bundle_id=str(last_activation.get("bundle_id") or ""),
            app_name=str(last_activation.get("app_name") or last_activation.get("bundle_id") or ""),
            canonical_id=canonical_id,
            display_name=str(
                last_activation.get("display_name")
                or last_activation.get("app_name")
                or canonical_id
            ),
            platform=str(last_activation.get("platform") or ""),
            start_at=start_at,
            end_at=end_at,
        )

    async def apply_activation(
        self,
        *,
        runtime_paths: Any,
        occurred_at: datetime,
        bundle_id: str,
        app_name: str,
        canonical_id: str | None = None,
        display_name: str | None = None,
        platform: str = "",
    ) -> None:
        """Record a new foreground app activation observed at ``occurred_at``.

        ``canonical_id`` / ``display_name`` default to ``bundle_id`` / ``app_name``
        when not provided so legacy callers (and tests) keep working without
        having to wire the catalog.
        """
        canonical_id = canonical_id or bundle_id
        display_name = display_name or app_name or bundle_id
        path = self._state_path(runtime_paths)
        async with self._lock_for(path):
            state = self._load_state(path)
            open_buckets = dict(state.get("open_buckets") or {})
            last_activation = state.get("last_activation")

            if (
                isinstance(last_activation, dict)
                and (last_activation.get("canonical_id") or last_activation.get("bundle_id"))
                and last_activation.get("observed_at")
            ):
                normalized_last = dict(last_activation)
                normalized_last.setdefault(
                    "canonical_id", last_activation.get("bundle_id") or ""
                )
                normalized_last.setdefault(
                    "display_name",
                    last_activation.get("app_name") or normalized_last["canonical_id"],
                )
                self._carry_forward_last_activation(
                    open_buckets=open_buckets,
                    last_activation=normalized_last,
                    end_at=occurred_at,
                )

            previous_canonical = (
                str(last_activation.get("canonical_id") or last_activation.get("bundle_id") or "")
                if isinstance(last_activation, dict)
                else ""
            )
            previous_session_id = (
                str(last_activation.get("session_id") or "")
                if isinstance(last_activation, dict)
                else ""
            )
            if previous_canonical == canonical_id and previous_session_id:
                session_id = previous_session_id
            else:
                session_id = f"{int(occurred_at.timestamp() * 1000)}:{canonical_id}"

            next_state = {
                "last_activation": {
                    "session_id": session_id,
                    "bundle_id": bundle_id,
                    "app_name": app_name,
                    "canonical_id": canonical_id,
                    "display_name": display_name,
                    "platform": platform,
                    "observed_at": occurred_at.isoformat(),
                },
                "open_buckets": open_buckets,
            }
            self._save_state(path, next_state)

    async def flush_completed(
        self,
        *,
        runtime_paths: Any,
        now: datetime,
    ) -> list[dict[str, Any]]:
        """Return buckets whose end time has already passed and clear them."""
        path = self._state_path(runtime_paths)
        async with self._lock_for(path):
            state = self._load_state(path)
            open_buckets = dict(state.get("open_buckets") or {})
            last_activation = state.get("last_activation")

            if isinstance(last_activation, dict):
                self._carry_forward_last_activation(
                    open_buckets=open_buckets,
                    last_activation=last_activation,
                    end_at=now,
                )
                last_activation = dict(last_activation)
                last_activation["observed_at"] = now.isoformat()

            completed_before = self._floor_hour(now)
            completed: list[dict[str, Any]] = []
            remaining_buckets: dict[str, dict[str, Any]] = {}
            for key, bucket in open_buckets.items():
                bucket_end = datetime.fromisoformat(str(bucket["bucket_end"]))
                if bucket_end.tzinfo is None:
                    bucket_end = bucket_end.replace(tzinfo=timezone.utc)
                if bucket_end <= completed_before:
                    completed.append(
                        {
                            "bucket_start": str(bucket["bucket_start"]),
                            "bucket_end": str(bucket["bucket_end"]),
                            "bundle_id": str(bucket.get("bundle_id", "")),
                            "app_name": str(bucket.get("app_name", "")),
                            "canonical_id": str(
                                bucket.get("canonical_id") or bucket.get("bundle_id") or ""
                            ),
                            "display_name": str(
                                bucket.get("display_name")
                                or bucket.get("app_name")
                                or bucket.get("bundle_id")
                                or ""
                            ),
                            "platform": str(bucket.get("platform", "")),
                            "duration_seconds": int(bucket.get("duration_seconds", 0)),
                            "session_count": int(bucket.get("session_count", 0)),
                        }
                    )
                else:
                    remaining_buckets[key] = bucket

            next_state = {
                "last_activation": last_activation,
                "open_buckets": remaining_buckets,
            }
            self._save_state(path, next_state)

        completed.sort(key=lambda item: (item.get("bucket_start", ""), item.get("canonical_id", "")))
        return completed

    async def flush_in_progress(
        self,
        *,
        runtime_paths: Any,
        now: datetime,
    ) -> dict[str, Any]:
        """Materialize the open session into buckets without removing them."""
        path = self._state_path(runtime_paths)
        async with self._lock_for(path):
            state = self._load_state(path)
            open_buckets = dict(state.get("open_buckets") or {})
            last_activation = state.get("last_activation")

            if isinstance(last_activation, dict):
                self._carry_forward_last_activation(
                    open_buckets=open_buckets,
                    last_activation=last_activation,
                    end_at=now,
                )
                last_activation = dict(last_activation)
                last_activation["observed_at"] = now.isoformat()

            next_state = {
                "last_activation": last_activation,
                "open_buckets": open_buckets,
            }
            self._save_state(path, next_state)

        return {
            "bucket_count": len(open_buckets),
            "active_canonical_id": (
                str(last_activation.get("canonical_id") or last_activation.get("bundle_id"))
                if isinstance(last_activation, dict)
                and (last_activation.get("canonical_id") or last_activation.get("bundle_id"))
                else None
            ),
            "flushed_at": now.isoformat(),
        }
