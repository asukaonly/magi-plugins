"""Tests for the adapter base: Conversation dataclass + select_adapter.

Loads ``adapters/base.py`` via the repo's synthesized-loader convention (mirrors
obsidian-vault / git_activity tests) rather than a sys.path import, so it
exercises the worktree copy directly. ``base.py`` is loaded as a submodule of a
synthetic ``adapters`` package whose ``__path__`` points at the adapters dir, so
``select_adapter``'s lazy ``from .claude_code import ...`` / ``from .codex import
...`` resolve to the sibling modules once those land (CAH-T3 / CAH-T4).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_base() -> ModuleType:
    adapters_dir = Path(__file__).resolve().parents[1] / "adapters"
    pkg = "coding_agent_history_adapters_under_test"
    # Register the synthetic adapters package first so relative imports inside
    # base.py (``from .claude_code import ...``) resolve against adapters_dir.
    pkg_spec = importlib.util.spec_from_file_location(
        pkg, adapters_dir / "__init__.py", submodule_search_locations=[str(adapters_dir)]
    )
    assert pkg_spec is not None and pkg_spec.loader is not None
    package = importlib.util.module_from_spec(pkg_spec)
    sys.modules[pkg] = package
    pkg_spec.loader.exec_module(package)

    base_spec = importlib.util.spec_from_file_location(f"{pkg}.base", adapters_dir / "base.py")
    assert base_spec is not None and base_spec.loader is not None
    module = importlib.util.module_from_spec(base_spec)
    sys.modules[base_spec.name] = module
    base_spec.loader.exec_module(module)
    return module


def test_conversation_shape() -> None:
    base = _load_base()
    c = base.Conversation(
        agent="claude_code",
        session_id="s1",
        occurred_at=1.0,
        user_turns=["hi"],
        project_hint="proj",
        native_path="/x.jsonl",
    )
    assert c.user_turns == ["hi"]
    assert c.session_id == "s1"
    assert c.agent == "claude_code"
    assert c.occurred_at == 1.0
    assert c.project_hint == "proj"
    assert c.native_path == "/x.jsonl"


def test_conversation_defaults() -> None:
    base = _load_base()
    c = base.Conversation(agent="codex", session_id="c1", occurred_at=2.0)
    assert c.user_turns == []
    assert c.project_hint is None
    assert c.native_path == ""


def test_select_adapter_by_path(tmp_path) -> None:
    base = _load_base()
    claude = tmp_path / ".claude" / "projects"
    claude.mkdir(parents=True)
    codex = tmp_path / ".codex"
    codex.mkdir()
    assert base.select_adapter(str(claude)).agent == "claude_code"
    assert base.select_adapter(str(codex)).agent == "codex"
    assert base.select_adapter(str(tmp_path / "nope")) is None
