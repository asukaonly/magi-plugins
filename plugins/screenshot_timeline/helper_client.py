"""Async stdio JSON client for the Swift vision helper."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class HelperError(Exception):
    pass


class HelperTimeoutError(HelperError):
    pass


class HelperCrashedError(HelperError):
    pass


@dataclass
class HelperClient:
    binary_argv: list[str]
    request_timeout: float = 10.0
    restart_initial_delay: float = 1.0
    restart_max_delay: float = 60.0
    _proc: asyncio.subprocess.Process | None = field(default=None, init=False)
    _read_task: asyncio.Task | None = field(default=None, init=False)
    _stderr_task: asyncio.Task | None = field(default=None, init=False)
    _pending: dict[str, asyncio.Future] = field(default_factory=dict, init=False)
    _restart_delay: float = field(default=0.0, init=False)
    _shutdown_requested: bool = field(default=False, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _alive_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    async def start(self) -> None:
        async with self._lock:
            if self._proc is not None:
                return
            await self._spawn()

    async def _spawn(self) -> None:
        logger.info("helper.spawn argv=%s", self.binary_argv)
        self._proc = await asyncio.create_subprocess_exec(
            *self.binary_argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        self._restart_delay = self.restart_initial_delay
        self._alive_event.set()
        self._read_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())

    async def shutdown(self) -> None:
        self._shutdown_requested = True
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.write((json.dumps({"id": "shutdown", "op": "shutdown"}) + "\n").encode())
                await proc.stdin.drain()
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                proc.kill()
        # Fail any pending requests
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(HelperCrashedError("helper shut down"))
        self._pending.clear()
        for task in (self._read_task, self._stderr_task):
            if task and not task.done():
                task.cancel()

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self.start()
        rid = str(payload["id"])
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        try:
            proc.stdin.write((json.dumps(payload) + "\n").encode())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(rid, None)
            raise HelperCrashedError("helper stdin closed") from exc
        try:
            return await asyncio.wait_for(fut, timeout=self.request_timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(rid, None)
            raise HelperTimeoutError(f"helper did not respond within {self.request_timeout}s for id={rid}") from exc

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        proc = self._proc
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    resp = json.loads(line.decode().strip())
                except json.JSONDecodeError:
                    logger.warning("helper.bad_json line=%r", line)
                    continue
                rid = str(resp.get("id") or "")
                fut = self._pending.pop(rid, None)
                if fut and not fut.done():
                    fut.set_result(resp)
        finally:
            await self._on_helper_exit()

    async def _stderr_loop(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        proc = self._proc
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                logger.warning("helper.stderr %s", line.decode().rstrip())
        except asyncio.CancelledError:
            pass

    async def _on_helper_exit(self) -> None:
        proc = self._proc
        self._proc = None
        self._alive_event.clear()
        # Fail any in-flight requests with crashed error
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(HelperCrashedError("helper exited"))
        self._pending.clear()
        if self._shutdown_requested:
            return
        # Respawn with exponential backoff
        delay = self._restart_delay
        logger.warning("helper.exited rc=%s respawn_in=%.1fs",
                       proc.returncode if proc else None, delay)
        await asyncio.sleep(delay)
        self._restart_delay = min(self.restart_max_delay, max(self.restart_initial_delay, delay * 2))
        try:
            async with self._lock:
                if self._shutdown_requested:
                    return
                await self._spawn()
        except Exception:  # noqa: BLE001
            logger.exception("helper.respawn_failed")


__all__ = ["HelperClient", "HelperError", "HelperTimeoutError", "HelperCrashedError"]
