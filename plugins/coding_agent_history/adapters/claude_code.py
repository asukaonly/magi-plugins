"""Claude Code transcript adapter.

Claude Code stores one JSONL transcript per session under
``~/.claude/projects/<encoded-project-path>/<session>.jsonl``. Each line is one
event; we want **only the user's own typed turns**.

The crux: a genuine user turn is a line with ``type == "user"`` AND
``message.content`` that is a **str**. When ``content`` is a **list** it is a
``tool_result`` -- the agent feeding a tool's output back as a synthetic user
message -- and must be skipped (it is not something the user wrote). Assistant
lines (``type == "assistant"``) are likewise skipped.

``occurred_at`` is the max user-turn ISO timestamp in the session.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .base import Conversation


def _iso_to_ts(value: str) -> float:
    """Parse an ISO-8601 timestamp (``...Z`` or offset) to unix seconds; 0.0 on failure."""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


class ClaudeCodeAdapter:
    """Normalizes Claude Code session JSONL files into ``Conversation`` records."""

    agent = "claude_code"

    def matches(self, path: str) -> bool:
        """True for a Claude Code projects root (``.../.claude/projects`` or similar).

        Recognizes a ``projects`` directory living under ``.claude``, or -- as a
        loose fallback -- any path whose parts contain both ``claude`` and
        ``projects`` (handles symlinked / relocated layouts). Deliberately does
        NOT match a bare ``.codex`` path, which is the Codex adapter's domain.
        """
        p = Path(path).expanduser()
        if p.name == "projects" and p.parent.name == ".claude":
            return True
        parts = [part.lower() for part in p.parts]
        return any("claude" in part for part in parts) and "projects" in parts

    def iter_conversations(
        self, path: str, *, since_mtime: float, cutoff_ts: float
    ) -> Iterable[Conversation]:
        """Yield one ``Conversation`` per session JSONL under ``path``.

        - Files whose mtime is ``<= since_mtime`` are skipped (forward-incremental
          cursor): nothing changed since the last sync.
        - A session whose newest genuine user turn predates ``cutoff_ts`` is
          dropped (first-sync lookback window).
        - Sessions with no genuine user turns are omitted entirely.
        Unreadable / malformed files and lines are skipped, never fatal.
        """
        root = Path(path).expanduser()
        if not root.is_dir():
            return
        for jsonl in sorted(root.rglob("*.jsonl")):
            try:
                if jsonl.stat().st_mtime <= since_mtime:
                    continue
            except OSError:
                continue

            session_id = ""
            project_hint = jsonl.parent.name
            turns: list[str] = []
            max_ts = 0.0
            try:
                with jsonl.open("r", encoding="utf-8") as handle:
                    for raw_line in handle:
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(obj, dict) or obj.get("type") != "user":
                            continue
                        message = obj.get("message") or {}
                        content = message.get("content") if isinstance(message, dict) else None
                        # list content == tool_result feedback -> NOT a genuine
                        # user turn; only a non-empty str is something the user typed.
                        if not isinstance(content, str) or not content.strip():
                            continue
                        turns.append(content.strip())
                        if not session_id:
                            session_id = str(obj.get("sessionId") or jsonl.stem)
                        max_ts = max(max_ts, _iso_to_ts(str(obj.get("timestamp") or "")))
            except OSError:
                continue

            if not turns:
                continue
            if cutoff_ts and max_ts and max_ts < cutoff_ts:
                continue

            try:
                fallback_mtime = jsonl.stat().st_mtime
            except OSError:
                fallback_mtime = 0.0
            yield Conversation(
                agent=self.agent,
                session_id=session_id or jsonl.stem,
                occurred_at=max_ts or fallback_mtime,
                user_turns=turns,
                project_hint=project_hint,
                native_path=str(jsonl),
            )
