"""Tests for the helper client."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mock_helper.py"


def _load() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "helper_client.py"
    spec = importlib.util.spec_from_file_location("screenshot_timeline_helper_client", module_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # required for @dataclass under Python 3.12
    spec.loader.exec_module(mod)
    return mod


def _client(mod: ModuleType, **overrides):
    cmd = overrides.pop("cmd", [sys.executable, str(_FIXTURE)])
    return mod.HelperClient(binary_argv=cmd, **overrides)


@pytest.mark.asyncio
async def test_probe_active_window_returns_canned_payload() -> None:
    mod = _load()
    client = _client(mod)
    await client.start()
    try:
        resp = await client.request({"id": "req_1", "op": "probe_active_window"})
        assert resp["ok"] is True
        assert resp["active_window"]["app_bundle_id"] == "com.apple.Safari"
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_request_id_correlation() -> None:
    mod = _load()
    client = _client(mod)
    await client.start()
    try:
        results = await asyncio.gather(
            client.request({"id": "a", "op": "probe_active_window"}),
            client.request({"id": "b", "op": "probe_active_window"}),
        )
        assert results[0]["id"] == "a"
        assert results[1]["id"] == "b"
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_request_timeout() -> None:
    mod = _load()
    client = _client(mod, request_timeout=0.5)
    await client.start()
    try:
        with pytest.raises(mod.HelperTimeoutError):
            await client.request({"id": "hang_1", "op": "hang"})
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_crash_triggers_respawn() -> None:
    mod = _load()
    client = _client(mod, restart_initial_delay=0.05, restart_max_delay=0.1, request_timeout=2.0)
    await client.start()
    try:
        # Send a crash request — helper exits
        with pytest.raises(mod.HelperCrashedError):
            await client.request({"id": "die", "op": "crash"})
        # Give the supervisor a tick to respawn
        await asyncio.sleep(0.3)
        # Next request should succeed against the new process
        resp = await client.request({"id": "after", "op": "probe_active_window"})
        assert resp["ok"] is True
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_large_response_line_is_read() -> None:
    """A helper response larger than asyncio's default 64KB readline limit must
    be read intact, not crash the read loop. The crash previously killed the
    response channel and made every subsequent probe time out."""
    mod = _load()
    client = _client(mod, request_timeout=2.0)
    await client.start()
    try:
        resp = await client.request({"id": "big_1", "op": "big", "size": 200 * 1024})
        assert resp["ok"] is True
        assert len(resp["blob"]) == 200 * 1024
        # The channel must still work afterwards (read loop didn't die).
        resp2 = await client.request({"id": "after_big", "op": "probe_active_window"})
        assert resp2["ok"] is True
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_recycles_wedged_helper_after_consecutive_timeouts() -> None:
    """A helper that stops responding must be recycled (killed + respawned)
    after max_consecutive_timeouts, so a fresh helper serves later probes
    instead of timing out forever against the wedged process."""
    mod = _load()
    client = _client(
        mod,
        request_timeout=0.3,
        max_consecutive_timeouts=2,
        restart_initial_delay=0.05,
        restart_max_delay=0.1,
    )
    await client.start()
    try:
        first_pid = client._proc.pid
        for i in range(2):
            with pytest.raises(mod.HelperTimeoutError):
                await client.request({"id": f"hang_{i}", "op": "hang"})
        # Supervisor respawns a fresh helper after the watchdog recycles.
        await asyncio.sleep(0.4)
        resp = await client.request({"id": "after_hang", "op": "probe_active_window"})
        assert resp["ok"] is True
        assert client._proc is not None and client._proc.pid != first_pid
    finally:
        await client.shutdown()
