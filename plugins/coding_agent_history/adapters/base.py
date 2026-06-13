"""Adapter seam for coding-agent transcripts.

Each agent (Claude Code, Codex, ...) has its own on-disk transcript format. An
``Adapter`` normalizes that format into a stream of ``Conversation`` records --
one per session, carrying *only the user's own turns* -- which the sensor then
scrubs and emits as first-person (``author_type="user"``) events.

``select_adapter`` picks the right adapter for a configured source path. It
imports the concrete adapters lazily so this module stays import-cycle-free
(each concrete adapter imports ``Conversation`` from here).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Protocol, runtime_checkable


@dataclass(slots=True)
class Conversation:
    """One normalized coding-agent session: the user's own turns + metadata."""

    agent: str
    session_id: str
    occurred_at: float  # unix seconds (max user-turn ts in the session)
    user_turns: list[str] = field(default_factory=list)
    project_hint: Optional[str] = None
    native_path: str = ""


@runtime_checkable
class Adapter(Protocol):
    """Normalizes one agent's transcript format into ``Conversation`` records."""

    agent: str

    def matches(self, path: str) -> bool:
        """Return True if this adapter understands the given source path."""
        ...

    def iter_conversations(
        self, path: str, *, since_mtime: float, cutoff_ts: float
    ) -> Iterable[Conversation]:
        """Yield conversations under ``path`` newer than ``since_mtime`` / ``cutoff_ts``."""
        ...


def select_adapter(path: str) -> Optional[Adapter]:
    """Return the first adapter that matches ``path``, or None.

    Imports the concrete adapters lazily to avoid an import cycle (they import
    ``Conversation`` from this module).
    """
    from .claude_code import ClaudeCodeAdapter
    from .codex import CodexAdapter

    for adapter in (ClaudeCodeAdapter(), CodexAdapter()):
        if adapter.matches(path):
            return adapter
    return None
