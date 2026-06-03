"""Async stdio JSON client for the Swift vision helper.

The underlying subprocess is spawned via `magi_plugin_sdk.ManagedSubprocess`,
which registers the helper's PID in a host-wide registry. If the backend
dies unexpectedly, the next boot will sweep any surviving helper via
`ManagedSubprocess.cleanup_orphans()` (called from the backend lifecycle).

We pair that with the helper's own stdin-EOF self-exit (see main.swift) so
this works without depending on a single mechanism.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

try:
    from magi_plugin_sdk.subprocess import ManagedSubprocess  # type: ignore
except ImportError:  # pragma: no cover — SDK should always be present
    ManagedSubprocess = None  # type: ignore[assignment,misc]

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
    # After this many consecutive request timeouts the helper is presumed
    # wedged (not responding) and gets recycled (killed + respawned) instead
    # of timing out forever against a hung process.
    max_consecutive_timeouts: int = 3
    # Label used in the ManagedSubprocess PID registry. Set per-instance if
    # you spawn multiple helpers from the same plugin.
    managed_label: str = "screenshot_timeline.helper"
    _managed: Any | None = field(default=None, init=False)  # ManagedSubprocess
    _consecutive_timeouts: int = field(default=0, init=False)
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
        if ManagedSubprocess is not None:
            # Normal path — SDK-managed subprocess writes to the PID
            # registry so crash-recovery can clean up an orphan helper.
            self._managed = await ManagedSubprocess.spawn(
                list(self.binary_argv),
                label=self.managed_label,
                env=os.environ.copy(),
            )
            self._proc = self._managed.proc
        else:
            # Fallback if SDK isn't importable for some reason (shouldn't
            # happen in production but keeps tests independent of the
            # registry path). The helper still self-exits on stdin EOF,
            # so this is degraded-but-functional.
            self._managed = None
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
        managed = self._managed
        proc = self._proc
        self._managed = None
        self._proc = None
        if proc is None:
            return
        # Send our protocol-level shutdown op first so the helper has a
        # chance to flush. Then ManagedSubprocess.shutdown() owns the
        # stdin-EOF → SIGTERM → SIGKILL escalation and deregistration.
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.write((json.dumps({"id": "shutdown", "op": "shutdown"}) + "\n").encode())
                await proc.stdin.drain()
        except Exception:  # noqa: BLE001
            pass
        if managed is not None:
            try:
                await managed.shutdown(sigterm_grace_seconds=2.0, sigkill_grace_seconds=1.0)
            except Exception:  # noqa: BLE001
                logger.exception("helper.shutdown_failed")
        else:
            # Fallback path (no SDK) — inline what ManagedSubprocess does.
            try:
                if proc.stdin and not proc.stdin.is_closing():
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
            result = await asyncio.wait_for(fut, timeout=self.request_timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(rid, None)
            self._consecutive_timeouts += 1
            if self._consecutive_timeouts >= self.max_consecutive_timeouts:
                # Helper is wedged — kill it so the read loop EOFs and the
                # supervisor respawns a fresh one. Reset so we don't recycle
                # again until the replacement also wedges.
                self._consecutive_timeouts = 0
                logger.warning(
                    "helper.unresponsive recycling after %d consecutive timeouts",
                    self.max_consecutive_timeouts,
                )
                await self._recycle()
            raise HelperTimeoutError(
                f"helper did not respond within {self.request_timeout}s for id={rid}"
            ) from exc
        self._consecutive_timeouts = 0
        return result

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        # Manual newline framing over read(): asyncio StreamReader.readline()
        # raises LimitOverrunError once a line exceeds its 64KB limit, which
        # used to crash this loop and kill the response channel (every probe
        # then timed out at 10s). read() has no per-line limit, so large helper
        # responses (big AX trees / OCR payloads) are read intact.
        buf = bytearray()
        try:
            while True:
                chunk = await stdout.read(65536)
                if not chunk:
                    break  # EOF — helper closed stdout / exited
                buf.extend(chunk)
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(buf[:nl])
                    del buf[: nl + 1]
                    self._handle_line(line)
        finally:
            await self._on_helper_exit()

    def _handle_line(self, line: bytes) -> None:
        text = line.decode(errors="replace").strip()
        if not text:
            return
        try:
            resp = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("helper.bad_json line=%r", line[:200])
            return
        rid = str(resp.get("id") or "")
        fut = self._pending.pop(rid, None)
        if fut and not fut.done():
            # A real response proves the helper is alive — clear the wedged
            # counter so one slow turn doesn't later trip the watchdog.
            self._consecutive_timeouts = 0
            fut.set_result(resp)

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

    async def _kill_proc(self, proc: Any, managed: Any) -> None:
        """Force-terminate an abandoned helper process.

        A read-loop exit can leave the OS process alive (a crashed reader, or a
        helper that merely closed stdout). Without an explicit kill those linger
        as spinning orphan processes that accumulate across respawns.
        """
        if managed is not None:
            try:
                await managed.shutdown(sigterm_grace_seconds=1.0, sigkill_grace_seconds=1.0)
                return
            except Exception:  # noqa: BLE001
                logger.warning("helper.kill_managed_failed", exc_info=True)
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:  # noqa: BLE001
                logger.warning("helper.kill_failed", exc_info=True)

    async def _recycle(self) -> None:
        """Kill a wedged (non-responding) helper. Its stdout then EOFs, which
        drives ``_read_loop`` -> ``_on_helper_exit`` -> respawn."""
        await self._kill_proc(self._proc, self._managed)

    async def _on_helper_exit(self) -> None:
        proc = self._proc
        managed = self._managed
        self._proc = None
        self._managed = None
        self._alive_event.clear()
        # Ensure the OS process is actually dead before respawning so a
        # crashed/closed read loop can't leak a spinning orphan helper.
        await self._kill_proc(proc, managed)
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
