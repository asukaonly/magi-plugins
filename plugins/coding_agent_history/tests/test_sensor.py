"""Tests for the coding-agent history sensor.

Loads the plugin package + ``sensor.py`` via the repo's synthesized-loader
convention (mirrors obsidian-vault/tests/test_sensor.py): a synthetic parent
package whose ``__path__`` points at the plugin dir, so ``sensor.py``'s
package-relative imports (``from .adapters.base import select_adapter`` /
``from .scrub import redact_secrets``) resolve against the worktree copy without
putting ``plugins/`` on sys.path.

THE CRUX (asserted in ``test_policy_marks_user_authored``): the sensor's
``memory_policy`` sets ``author_type="user"`` + ``memory_domain="user_authored"``
(mirroring obsidian-vault) so L2 tags the ingested content ``[USER]`` and mines
it into the user's own profile -- the whole point of this sensor.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType


def _load_sensor_module() -> ModuleType:
    plugin_dir = Path(__file__).resolve().parents[1]
    pkg_name = "coding_agent_history_under_test"
    if pkg_name not in sys.modules:
        pkg_spec = importlib.util.spec_from_file_location(
            pkg_name, plugin_dir / "__init__.py", submodule_search_locations=[str(plugin_dir)]
        )
        assert pkg_spec is not None and pkg_spec.loader is not None
        package = importlib.util.module_from_spec(pkg_spec)
        sys.modules[pkg_name] = package
        pkg_spec.loader.exec_module(package)

    mod_name = f"{pkg_name}.sensor"
    spec = importlib.util.spec_from_file_location(mod_name, plugin_dir / "sensor.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _ctx(tmp_path: Path, paths, *, source_type="codex_agent_history", lookback=30, last_cursor=None, limit=1000):
    """Build a real SensorSyncContext; runtime_paths is the SDK's path facade."""
    from magi_plugin_sdk.sensors import SensorSyncContext

    class _Paths:
        def plugin_cache_dir(self, plugin_id: str) -> Path:
            return tmp_path / "cache"

    return SensorSyncContext(
        source_type=source_type,
        manual=True,
        last_cursor=last_cursor,
        last_success_at=None,
        limit=limit,
        runtime_paths=_Paths(),
        plugin_settings={
            "sensors": {
                source_type: {
                    "source_paths": list(paths),
                    "initial_sync_lookback_days": lookback,
                }
            }
        },
    )


def test_policy_marks_user_authored() -> None:
    mod = _load_sensor_module()
    sensor = mod.CodingAgentHistorySensor(
        agent="codex",
        source_type="codex_agent_history",
        display_name="Codex",
    )
    # THE CRUX: author_type="user" => L2 renders [USER] and extracts these as the
    # user's own facts (mirrors obsidian-vault). memory_domain confirms the route.
    assert sensor.memory_policy.author_type == "user"
    assert sensor.memory_policy.memory_domain == "user_authored"
    assert sensor.source_type == "codex_agent_history"
    assert sensor.supports_pull_sync is True


def test_collect_and_build_output_scrubs_and_uses_user_turns(tmp_path: Path) -> None:
    mod = _load_sensor_module()
    root = tmp_path / ".codex"
    root.mkdir()
    now = time.time()
    (root / "history.jsonl").write_text(
        "\n".join(
            json.dumps(o)
            for o in [
                {
                    "session_id": "c1",
                    "text": "deploy with token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
                    "ts": now,
                },
                {"session_id": "c1", "text": "and refactor the parser", "ts": now},
            ]
        ),
        encoding="utf-8",
    )
    sensor = mod.CodingAgentHistorySensor(
        agent="codex",
        source_type="codex_agent_history",
        display_name="Codex",
    )
    res = asyncio.run(sensor.collect_items(_ctx(tmp_path, [str(root)])))
    assert len(res.items) == 1
    item = res.items[0]
    assert item["source_item_id"] == "codex:c1"

    out = asyncio.run(sensor.build_output(item))
    # pinned_payload holds the FULL scrubbed user-turn text for L2.
    pinned = out.pinned_payload or ""
    body = pinned + " " + out.narration.body
    assert "ghp_ABCDEFGHIJ" not in body  # secret scrubbed before it leaves the sensor
    assert "[REDACTED" in pinned
    assert "refactor the parser" in pinned  # ordinary prose preserved
    assert out.source_item_id == "codex:c1"
    assert out.source_type == "codex_agent_history"
    assert res.next_cursor is not None


def test_agent_filter_keeps_entries_separate(tmp_path: Path) -> None:
    mod = _load_sensor_module()
    now = time.time()

    codex_root = tmp_path / ".codex"
    codex_root.mkdir()
    (codex_root / "history.jsonl").write_text(
        json.dumps({"session_id": "codex-1", "text": "work on codex adapter", "ts": now}),
        encoding="utf-8",
    )

    claude_root = tmp_path / ".claude" / "projects" / "-Users-me-proj"
    claude_root.mkdir(parents=True)
    (claude_root / "claude-1.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"content": "work on claude adapter"},
                "sessionId": "claude-1",
                "timestamp": "2026-06-22T12:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    codex_sensor = mod.CodingAgentHistorySensor(
        agent="codex",
        source_type="codex_agent_history",
        display_name="Codex",
    )
    claude_sensor = mod.CodingAgentHistorySensor(
        agent="claude_code",
        source_type="claude_code_agent_history",
        display_name="Claude Code",
    )

    paths = [str(codex_root), str(claude_root.parent)]
    codex_res = asyncio.run(codex_sensor.collect_items(_ctx(tmp_path, paths)))
    claude_res = asyncio.run(
        claude_sensor.collect_items(
            _ctx(tmp_path, paths, source_type="claude_code_agent_history")
        )
    )

    assert [item["source_item_id"] for item in codex_res.items] == ["codex:codex-1"]
    assert [item["source_item_id"] for item in claude_res.items] == ["claude_code:claude-1"]


def test_first_sync_windowed_excludes_old(tmp_path: Path) -> None:
    mod = _load_sensor_module()
    root = tmp_path / ".codex"
    root.mkdir()
    (root / "history.jsonl").write_text(
        json.dumps(
            {"session_id": "old", "text": "ancient prompt", "ts": time.time() - 60 * 60 * 24 * 365}
        ),
        encoding="utf-8",
    )
    sensor = mod.CodingAgentHistorySensor(
        agent="codex",
        source_type="codex_agent_history",
        display_name="Codex",
    )
    res = asyncio.run(
        sensor.collect_items(_ctx(tmp_path, [str(root)], lookback=30, last_cursor=None))
    )
    # Older than the 30-day first-sync window => excluded on the first sync.
    assert res.items == []


def test_incremental_sync_ignores_lookback_window(tmp_path: Path) -> None:
    """A later (cursored) sync is forward-incremental, NOT windowed: an old session
    whose file changed since the cursor is still picked up (the lookback only gates
    the first sync)."""
    mod = _load_sensor_module()
    root = tmp_path / ".codex"
    root.mkdir()
    (root / "history.jsonl").write_text(
        json.dumps(
            {"session_id": "old", "text": "ancient but freshly written", "ts": time.time() - 60 * 60 * 24 * 365}
        ),
        encoding="utf-8",
    )
    sensor = mod.CodingAgentHistorySensor(
        agent="codex",
        source_type="codex_agent_history",
        display_name="Codex",
    )
    # last_cursor set (=> not first sync) and older than the file's mtime.
    res = asyncio.run(
        sensor.collect_items(_ctx(tmp_path, [str(root)], lookback=30, last_cursor="1.0"))
    )
    assert [it["source_item_id"] for it in res.items] == ["codex:old"]


def test_collect_marks_has_more_when_limit_is_full(tmp_path: Path) -> None:
    mod = _load_sensor_module()
    root = tmp_path / ".codex"
    root.mkdir()
    now = time.time()
    (root / "history.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"session_id": "one", "text": "first", "ts": now}),
                json.dumps({"session_id": "two", "text": "second", "ts": now + 1}),
            ]
        ),
        encoding="utf-8",
    )
    sensor = mod.CodingAgentHistorySensor(
        agent="codex",
        source_type="codex_agent_history",
        display_name="Codex",
    )

    res = asyncio.run(sensor.collect_items(_ctx(tmp_path, [str(root)], limit=1)))

    assert len(res.items) == 1
    assert res.stats["has_more"] is True


def test_dedup_identity_and_fingerprint() -> None:
    mod = _load_sensor_module()
    sensor = mod.CodingAgentHistorySensor(
        agent="codex",
        source_type="codex_agent_history",
        display_name="Codex",
    )
    a = {"source_item_id": "codex:c1", "user_turns": ["one"], "occurred_at": 1.0}
    b = {"source_item_id": "codex:c1", "user_turns": ["one", "two"], "occurred_at": 2.0}
    # Same conversation => same identity (supersession key)...
    assert sensor.source_item_identity(a) == sensor.source_item_identity(b) == "codex:c1"
    # ...but a grown session => different version fingerprint (re-ingest).
    assert sensor.source_item_version_fingerprint(a) != sensor.source_item_version_fingerprint(b)


def test_no_source_paths_returns_empty(tmp_path: Path) -> None:
    mod = _load_sensor_module()
    sensor = mod.CodingAgentHistorySensor(
        agent="codex",
        source_type="codex_agent_history",
        display_name="Codex",
    )
    res = asyncio.run(sensor.collect_items(_ctx(tmp_path, [])))
    assert res.items == []


def test_build_output_activity_and_narration_shape(tmp_path: Path) -> None:
    """build_output dispatches the SDK activity/narration helpers with the real
    (i18n_key-bearing) facet signature and pins the full scrubbed text."""
    mod = _load_sensor_module()
    sensor = mod.CodingAgentHistorySensor(
        agent="claude_code",
        source_type="claude_code_agent_history",
        display_name="Claude Code",
    )
    item = {
        "source_item_id": "claude_code:s1",
        "agent": "claude_code",
        "occurred_at": 1779000000.0,
        "user_turns": ["refactor the auth module", "now add a parser test"],
        "project_hint": "-Users-me-proj",
        "native_path": "/Users/me/.claude/projects/-Users-me-proj/s1.jsonl",
    }
    out = asyncio.run(sensor.build_output(item))
    assert out.occurred_at == 1779000000.0
    assert out.activity.source.code == "claude_code"
    assert out.activity.source.i18n_key  # facet requires a non-empty i18n_key
    assert out.activity.action.code
    assert out.activity.qualifiers["turn_count"] == 2
    # Full joined user-turn text is pinned for L2.
    assert "refactor the auth module" in (out.pinned_payload or "")
    assert "now add a parser test" in (out.pinned_payload or "")
    assert out.provenance["sensor_id"] == sensor.sensor_id
