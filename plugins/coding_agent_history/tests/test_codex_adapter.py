"""Tests for the Codex adapter.

Loads ``adapters/codex.py`` via the repo's synthesized-loader convention
(mirrors ``test_claude_adapter.py`` / ``test_adapters_base.py`` / obsidian-vault
/ git_activity tests) rather than a sys.path import, so it exercises the
worktree copy directly. ``codex.py`` is loaded as a submodule of a synthetic
``adapters`` package whose ``__path__`` points at the adapters dir, so its
``from .base import Conversation`` relative import resolves against the sibling
``base.py``.

Codex stores one user prompt per line in ``~/.codex/history.jsonl``
(``{session_id, text, ts(unix int)}``). One ``Conversation`` per session,
grouping that session's prompts in order, ``occurred_at`` = max ts.

Privacy crux: the adapter must read ONLY ``history.jsonl``. ``auth.json`` (and
the ``accounts/`` credential store) are NEVER opened -- guarded below by seeding
an ``auth.json`` with a fake secret and asserting it never leaks into output.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType


def _load_codex_adapter() -> ModuleType:
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

    mod_name = f"{pkg}.codex"
    spec = importlib.util.spec_from_file_location(mod_name, adapters_dir / "codex.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _write_history(codex_dir: Path, rows) -> Path:
    hist = codex_dir / "history.jsonl"
    hist.write_text("\n".join(json.dumps(o) for o in rows), encoding="utf-8")
    return hist


def test_groups_history_by_session(tmp_path: Path) -> None:
    mod = _load_codex_adapter()
    codex = tmp_path / ".codex"
    codex.mkdir()
    # An auth.json sitting right next to history.jsonl must NEVER be read.
    (codex / "auth.json").write_text(
        '{"token": "sk-should-never-be-read-AAAA1111BBBB2222"}', encoding="utf-8"
    )
    _write_history(
        codex,
        [
            {"session_id": "c1", "text": "add a parser test", "ts": 1779000000},
            {"session_id": "c1", "text": "now run it", "ts": 1779000060},
            {"session_id": "c2", "text": "fix the lint error", "ts": 1779000200},
        ],
    )
    convs = sorted(
        mod.CodexAdapter().iter_conversations(str(codex), since_mtime=0.0, cutoff_ts=0.0),
        key=lambda c: c.session_id,
    )
    assert [c.session_id for c in convs] == ["c1", "c2"]
    assert convs[0].agent == "codex"
    # prompts within a session preserved in file order
    assert convs[0].user_turns == ["add a parser test", "now run it"]
    # occurred_at == max ts in the session
    assert convs[0].occurred_at == 1779000060
    assert convs[1].user_turns == ["fix the lint error"]
    assert convs[1].occurred_at == 1779000200
    # The auth.json secret must never appear in any produced conversation.
    leaked = "sk-should-never-be-read-AAAA1111BBBB2222"
    for c in convs:
        assert leaked not in " ".join(c.user_turns)
        assert "auth.json" not in c.native_path
        assert c.native_path.endswith("history.jsonl")


def test_reads_codex_session_transcripts(tmp_path: Path) -> None:
    mod = _load_codex_adapter()
    codex = tmp_path / ".codex"
    session_dir = codex / "sessions" / "2026" / "06" / "25"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "rollout-2026-06-25T10-00-00-session.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(o)
            for o in [
                {
                    "type": "session_meta",
                    "timestamp": "2026-06-25T10:00:00.000Z",
                    "payload": {
                        "id": "session-1",
                        "cwd": "/Users/me/project",
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-06-25T10:01:00.000Z",
                    "payload": {
                        "type": "user_message",
                        "message": "build the Codex importer",
                        "images": [],
                        "local_images": [],
                        "text_elements": [],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-06-25T10:02:00.000Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "assistant text is ignored"}
                        ],
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-06-25T10:03:00.000Z",
                    "payload": {
                        "type": "user_message",
                        "message": "verify it with tests",
                        "images": [],
                        "local_images": [],
                        "text_elements": [],
                    },
                },
            ]
        ),
        encoding="utf-8",
    )

    convs = list(mod.CodexAdapter().iter_conversations(str(codex), since_mtime=0.0, cutoff_ts=0.0))

    assert len(convs) == 1
    assert convs[0].session_id == "session-1"
    assert convs[0].user_turns == ["build the Codex importer", "verify it with tests"]
    assert convs[0].project_hint == "project"
    assert convs[0].native_path == str(transcript)
    assert convs[0].occurred_at > 0


def test_auth_json_is_never_opened(tmp_path: Path, monkeypatch) -> None:
    """Hard guard: fail if the adapter opens any file other than history.jsonl."""
    mod = _load_codex_adapter()
    codex = tmp_path / ".codex"
    codex.mkdir()
    (codex / "auth.json").write_text('{"token": "secret"}', encoding="utf-8")
    _write_history(codex, [{"session_id": "c1", "text": "hello", "ts": 1779000000}])

    real_open = Path.open
    opened: list[str] = []

    def _tracking_open(self: Path, *args, **kwargs):
        opened.append(self.name)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _tracking_open)
    list(mod.CodexAdapter().iter_conversations(str(codex), since_mtime=0.0, cutoff_ts=0.0))
    assert "auth.json" not in opened
    assert opened == ["history.jsonl"] or opened == []


def test_cutoff_excludes_old_sessions(tmp_path: Path) -> None:
    mod = _load_codex_adapter()
    codex = tmp_path / ".codex"
    codex.mkdir()
    _write_history(codex, [{"session_id": "old", "text": "ancient prompt", "ts": 1000}])
    # cutoff in the future -> the (old) session is excluded by occurred_at
    assert (
        list(mod.CodexAdapter().iter_conversations(str(codex), since_mtime=0.0, cutoff_ts=time.time()))
        == []
    )


def test_since_mtime_skips_unchanged_history(tmp_path: Path) -> None:
    mod = _load_codex_adapter()
    codex = tmp_path / ".codex"
    codex.mkdir()
    _write_history(codex, [{"session_id": "c1", "text": "hi", "ts": 1779000000}])
    # since_mtime in the future -> file skipped before parsing (forward-incremental cursor)
    assert (
        list(
            mod.CodexAdapter().iter_conversations(
                str(codex), since_mtime=time.time() + 1000, cutoff_ts=0.0
            )
        )
        == []
    )


def test_skips_rows_missing_session_or_text(tmp_path: Path) -> None:
    mod = _load_codex_adapter()
    codex = tmp_path / ".codex"
    codex.mkdir()
    _write_history(
        codex,
        [
            {"session_id": "", "text": "no session", "ts": 1779000000},
            {"session_id": "c1", "text": "", "ts": 1779000001},
            {"session_id": "c1", "text": "   ", "ts": 1779000002},
            {"session_id": "c1", "text": "real prompt", "ts": 1779000003},
        ],
    )
    convs = list(mod.CodexAdapter().iter_conversations(str(codex), since_mtime=0.0, cutoff_ts=0.0))
    assert len(convs) == 1
    assert convs[0].session_id == "c1"
    assert convs[0].user_turns == ["real prompt"]


def test_malformed_lines_are_skipped(tmp_path: Path) -> None:
    mod = _load_codex_adapter()
    codex = tmp_path / ".codex"
    codex.mkdir()
    hist = codex / "history.jsonl"
    hist.write_text(
        "\n".join(
            [
                "not json at all",
                json.dumps({"session_id": "c1", "text": "good prompt", "ts": 1779000000}),
                "",
                "{broken json",
            ]
        ),
        encoding="utf-8",
    )
    convs = list(mod.CodexAdapter().iter_conversations(str(codex), since_mtime=0.0, cutoff_ts=0.0))
    assert len(convs) == 1
    assert convs[0].user_turns == ["good prompt"]


def test_matches_codex_path(tmp_path: Path) -> None:
    mod = _load_codex_adapter()
    codex = tmp_path / ".codex"
    codex.mkdir()
    claude_projects = tmp_path / ".claude" / "projects"
    claude_projects.mkdir(parents=True)
    adapter = mod.CodexAdapter()
    assert adapter.matches(str(codex)) is True
    assert adapter.matches(str(claude_projects)) is False


def test_missing_history_yields_nothing(tmp_path: Path) -> None:
    mod = _load_codex_adapter()
    codex = tmp_path / ".codex"
    codex.mkdir()  # no history.jsonl written
    assert (
        list(mod.CodexAdapter().iter_conversations(str(codex), since_mtime=0.0, cutoff_ts=0.0)) == []
    )
