"""Tests for the Claude Code adapter.

Loads ``adapters/claude_code.py`` via the repo's synthesized-loader convention
(mirrors ``test_adapters_base.py`` / obsidian-vault / git_activity tests) rather
than a sys.path import, so it exercises the worktree copy directly.
``claude_code.py`` is loaded as a submodule of a synthetic ``adapters`` package
whose ``__path__`` points at the adapters dir, so its ``from .base import
Conversation`` relative import resolves against the sibling ``base.py``.

The crux this guards: a *genuine* user turn is a line with ``type=="user"`` AND
``message.content`` that is a **str**. When ``content`` is a **list** it is a
``tool_result`` (the agent feeding a tool's output back as a synthetic user
message) and MUST be skipped.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType


def _load_claude_adapter() -> ModuleType:
    adapters_dir = Path(__file__).resolve().parents[1] / "adapters"
    pkg = "coding_agent_history_adapters_under_test"
    if pkg not in sys.modules:
        pkg_spec = importlib.util.spec_from_file_location(
            pkg, adapters_dir / "__init__.py", submodule_search_locations=[str(adapters_dir)]
        )
        assert pkg_spec is not None and pkg_spec.loader is not None
        package = importlib.util.module_from_spec(pkg_spec)
        sys.modules[pkg] = package
        pkg_spec.loader.exec_module(package)

    mod_name = f"{pkg}.claude_code"
    spec = importlib.util.spec_from_file_location(mod_name, adapters_dir / "claude_code.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _write(p: Path, lines) -> None:
    p.write_text("\n".join(json.dumps(o) for o in lines), encoding="utf-8")


def test_extracts_only_genuine_user_turns(tmp_path: Path) -> None:
    mod = _load_claude_adapter()
    root = tmp_path / ".claude" / "projects" / "-Users-me-proj"
    root.mkdir(parents=True)
    f = root / "sess1.jsonl"
    _write(
        f,
        [
            {
                "type": "user",
                "sessionId": "sess1",
                "timestamp": "2026-06-01T10:00:00.000Z",
                "message": {"role": "user", "content": "refactor the auth module"},
            },
            {
                "type": "assistant",
                "sessionId": "sess1",
                "timestamp": "2026-06-01T10:00:05.000Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "sure"}]},
            },
            {
                "type": "user",
                "sessionId": "sess1",
                "timestamp": "2026-06-01T10:00:09.000Z",
                # list content == tool_result feedback -> NOT a genuine user turn
                "message": {"role": "user", "content": [{"type": "tool_result", "content": "exit 0"}]},
            },
            {
                "type": "user",
                "sessionId": "sess1",
                "timestamp": "2026-06-01T10:01:00.000Z",
                "message": {"role": "user", "content": "now add a test"},
            },
        ],
    )
    convs = list(
        mod.ClaudeCodeAdapter().iter_conversations(str(root.parent), since_mtime=0.0, cutoff_ts=0.0)
    )
    assert len(convs) == 1
    c = convs[0]
    assert c.agent == "claude_code"
    assert c.session_id == "sess1"
    # assistant turn + tool_result user-line both excluded; genuine str turns kept in order
    assert c.user_turns == ["refactor the auth module", "now add a test"]
    assert c.occurred_at > 0
    assert c.native_path == str(f)


def test_blank_string_content_is_skipped(tmp_path: Path) -> None:
    mod = _load_claude_adapter()
    root = tmp_path / ".claude" / "projects" / "p"
    root.mkdir(parents=True)
    _write(
        root / "s.jsonl",
        [
            {
                "type": "user",
                "sessionId": "s",
                "timestamp": "2026-06-01T10:00:00.000Z",
                "message": {"role": "user", "content": "   "},
            },
            {
                "type": "user",
                "sessionId": "s",
                "timestamp": "2026-06-01T10:00:01.000Z",
                "message": {"role": "user", "content": "real prompt"},
            },
        ],
    )
    convs = list(
        mod.ClaudeCodeAdapter().iter_conversations(str(root.parent), since_mtime=0.0, cutoff_ts=0.0)
    )
    assert len(convs) == 1
    assert convs[0].user_turns == ["real prompt"]


def test_session_with_no_genuine_turns_is_omitted(tmp_path: Path) -> None:
    mod = _load_claude_adapter()
    root = tmp_path / ".claude" / "projects" / "p"
    root.mkdir(parents=True)
    _write(
        root / "toolonly.jsonl",
        [
            {
                "type": "user",
                "sessionId": "t",
                "timestamp": "2026-06-01T10:00:00.000Z",
                "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
            },
            {
                "type": "assistant",
                "sessionId": "t",
                "timestamp": "2026-06-01T10:00:01.000Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            },
        ],
    )
    convs = list(
        mod.ClaudeCodeAdapter().iter_conversations(str(root.parent), since_mtime=0.0, cutoff_ts=0.0)
    )
    assert convs == []


def test_multiple_sessions_each_yield_one_conversation(tmp_path: Path) -> None:
    mod = _load_claude_adapter()
    root = tmp_path / ".claude" / "projects" / "proj"
    root.mkdir(parents=True)
    _write(
        root / "a.jsonl",
        [
            {
                "type": "user",
                "sessionId": "a",
                "timestamp": "2026-06-01T10:00:00.000Z",
                "message": {"role": "user", "content": "one"},
            }
        ],
    )
    _write(
        root / "b.jsonl",
        [
            {
                "type": "user",
                "sessionId": "b",
                "timestamp": "2026-06-02T10:00:00.000Z",
                "message": {"role": "user", "content": "two"},
            }
        ],
    )
    convs = sorted(
        mod.ClaudeCodeAdapter().iter_conversations(str(root.parent), since_mtime=0.0, cutoff_ts=0.0),
        key=lambda c: c.session_id,
    )
    assert [c.session_id for c in convs] == ["a", "b"]
    assert convs[0].project_hint == "proj"
    assert convs[0].user_turns == ["one"]
    assert convs[1].user_turns == ["two"]


def test_skips_subagent_transcripts(tmp_path: Path) -> None:
    mod = _load_claude_adapter()
    root = tmp_path / ".claude" / "projects" / "proj"
    root.mkdir(parents=True)
    _write(
        root / "main.jsonl",
        [
            {
                "type": "user",
                "sessionId": "main",
                "timestamp": "2026-06-25T10:00:00.000Z",
                "isSidechain": False,
                "message": {"role": "user", "content": "top-level user prompt"},
            }
        ],
    )
    subagents = root / "main" / "subagents"
    subagents.mkdir(parents=True)
    _write(
        subagents / "agent-generated.jsonl",
        [
            {
                "type": "user",
                "sessionId": "agent-generated",
                "timestamp": "2026-06-25T10:01:00.000Z",
                "isSidechain": True,
                "message": {"role": "user", "content": "agent delegated prompt"},
            }
        ],
    )

    convs = list(
        mod.ClaudeCodeAdapter().iter_conversations(str(root.parent), since_mtime=0.0, cutoff_ts=0.0)
    )

    assert [conv.session_id for conv in convs] == ["main"]
    assert convs[0].user_turns == ["top-level user prompt"]


def test_mtime_and_cutoff_filters(tmp_path: Path) -> None:
    mod = _load_claude_adapter()
    root = tmp_path / ".claude" / "projects" / "p"
    root.mkdir(parents=True)
    _write(
        root / "old.jsonl",
        [
            {
                "type": "user",
                "sessionId": "old",
                "timestamp": "2020-01-01T00:00:00.000Z",
                "message": {"role": "user", "content": "ancient"},
            }
        ],
    )
    # cutoff in the future -> conversation excluded by occurred_at
    assert (
        list(
            mod.ClaudeCodeAdapter().iter_conversations(
                str(root.parent), since_mtime=0.0, cutoff_ts=time.time()
            )
        )
        == []
    )
    # since_mtime in the future -> file skipped before parsing
    assert (
        list(
            mod.ClaudeCodeAdapter().iter_conversations(
                str(root.parent), since_mtime=time.time() + 1000, cutoff_ts=0.0
            )
        )
        == []
    )


def test_matches_claude_projects_path(tmp_path: Path) -> None:
    mod = _load_claude_adapter()
    claude_projects = tmp_path / ".claude" / "projects"
    claude_projects.mkdir(parents=True)
    codex = tmp_path / ".codex"
    codex.mkdir()
    adapter = mod.ClaudeCodeAdapter()
    assert adapter.matches(str(claude_projects)) is True
    assert adapter.matches(str(codex)) is False


def test_missing_directory_yields_nothing(tmp_path: Path) -> None:
    mod = _load_claude_adapter()
    missing = tmp_path / "does_not_exist"
    assert (
        list(
            mod.ClaudeCodeAdapter().iter_conversations(str(missing), since_mtime=0.0, cutoff_ts=0.0)
        )
        == []
    )
