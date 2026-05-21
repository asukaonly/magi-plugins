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
