"""Codex transcript adapter.

Codex (the OpenAI coding agent) records one *user prompt per line* in
``~/.codex/history.jsonl``. Each line is ``{session_id, text, ts}`` where ``ts``
is unix seconds. We group lines by ``session_id`` into one ``Conversation`` per
session, preserving prompt order, with ``occurred_at`` = the max ts in the
session.

Privacy: this adapter reads ONLY ``history.jsonl``. It MUST NOT open
``~/.codex/auth.json`` or the ``accounts/`` credential store -- those hold the
user's API credentials and are off-limits. The richer ``session_index.jsonl`` /
``archived_sessions/`` are not needed for v1 (YAGNI) and are likewise left
untouched.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .base import Conversation


class CodexAdapter:
    """Normalizes ``~/.codex/history.jsonl`` user prompts into ``Conversation`` records."""

    agent = "codex"

    def matches(self, path: str) -> bool:
        """True for a Codex home (a ``.codex`` dir, or a dir holding ``history.jsonl``).

        Deliberately narrow: recognizes a directory literally named ``.codex`` or
        one that directly contains ``history.jsonl``. A bare Claude Code projects
        path (the other adapter's domain) does not match.
        """
        p = Path(path).expanduser()
        if p.name == ".codex":
            return True
        return (p / "history.jsonl").is_file()

    def iter_conversations(
        self, path: str, *, since_mtime: float, cutoff_ts: float
    ) -> Iterable[Conversation]:
        """Yield one ``Conversation`` per ``session_id`` found in ``history.jsonl``.

        - Only ``history.jsonl`` is opened (never ``auth.json`` / ``accounts/``).
        - If the file's mtime is ``<= since_mtime`` it is skipped wholesale
          (forward-incremental cursor): nothing changed since the last sync.
        - A session whose newest prompt predates ``cutoff_ts`` is dropped
          (first-sync lookback window).
        - Rows missing ``session_id`` or with empty ``text`` are skipped, as are
          malformed / unreadable lines. Read errors are never fatal.
        """
        root = Path(path).expanduser()
        hist = root / "history.jsonl" if root.is_dir() else root
        if not hist.is_file():
            return
        try:
            if hist.stat().st_mtime <= since_mtime:
                return
        except OSError:
            return

        # Preserve session insertion order (dict is insertion-ordered) and, within
        # each session, prompt order as encountered in the file.
        sessions: dict[str, dict] = {}
        try:
            with hist.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    session_id = str(obj.get("session_id") or "").strip()
                    text = str(obj.get("text") or "").strip()
                    if not session_id or not text:
                        continue
                    try:
                        ts = float(obj.get("ts") or 0.0)
                    except (TypeError, ValueError):
                        ts = 0.0
                    session = sessions.setdefault(session_id, {"turns": [], "max_ts": 0.0})
                    session["turns"].append(text)
                    session["max_ts"] = max(session["max_ts"], ts)
        except OSError:
            return

        native_path = str(hist)
        for session_id, session in sessions.items():
            max_ts = session["max_ts"]
            if cutoff_ts and max_ts and max_ts < cutoff_ts:
                continue
            yield Conversation(
                agent=self.agent,
                session_id=session_id,
                occurred_at=max_ts,
                user_turns=session["turns"],
                project_hint=None,
                native_path=native_path,
            )
