"""Codex transcript adapter.

Codex records local user prompts in two shapes:

- legacy ``~/.codex/history.jsonl`` rows: ``{session_id, text, ts}``
- session transcripts under ``~/.codex/sessions`` and ``archived_sessions``

Both shapes are grouped into one ``Conversation`` per session, preserving prompt
order, with ``occurred_at`` set to the newest user-turn timestamp.

Privacy: this adapter reads ONLY prompt/transcript JSONL files. It MUST NOT open
``~/.codex/auth.json`` or the ``accounts/`` credential store -- those hold the
user's API credentials and are off-limits.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .base import Conversation

_SESSION_DIRS = ("sessions", "archived_sessions")


def _iso_to_ts(value: str) -> float:
    """Parse an ISO-8601 timestamp to unix seconds; return 0.0 on failure."""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _content_texts(content: object) -> list[str]:
    """Extract text parts from a Codex message content payload."""
    if isinstance(content, str):
        stripped = content.strip()
        return [stripped] if stripped else []
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type") or "")
        if part_type and part_type not in {"input_text", "text"}:
            continue
        text = str(part.get("text") or "").strip()
        if text:
            texts.append(text)
    return texts


class CodexAdapter:
    """Normalizes local Codex user prompts into ``Conversation`` records."""

    agent = "codex"

    def matches(self, path: str) -> bool:
        """True for a Codex home or direct Codex transcript path.

        Deliberately narrow: recognizes a directory literally named ``.codex``,
        one that directly contains ``history.jsonl``, known Codex session
        directories, or JSONL files under a ``.codex`` tree. A bare Claude Code
        projects path (the other adapter's domain) does not match.
        """
        p = Path(path).expanduser()
        if p.name == ".codex":
            return True
        if p.name in _SESSION_DIRS and p.parent.name == ".codex":
            return True
        if p.is_file() and p.suffix == ".jsonl" and ".codex" in p.parts:
            return True
        return (p / "history.jsonl").is_file()

    def iter_conversations(
        self, path: str, *, since_mtime: float, cutoff_ts: float
    ) -> Iterable[Conversation]:
        """Yield one ``Conversation`` per Codex session.

        - Only JSONL prompt/session files are opened (never credentials).
        - If a file's mtime is ``<= since_mtime`` it is skipped wholesale
          (forward-incremental cursor): nothing changed since the last sync.
        - A session whose newest user prompt predates ``cutoff_ts`` is dropped
          (first-sync lookback window).
        - Rows missing ``session_id`` or with empty ``text`` are skipped, as are
          malformed / unreadable lines. Read errors are never fatal.
        """
        root = Path(path).expanduser()
        if root.is_file():
            if root.name == "history.jsonl":
                yield from self._iter_history_file(root, since_mtime, cutoff_ts)
            elif root.suffix == ".jsonl":
                conv = self._read_session_file(root, since_mtime, cutoff_ts)
                if conv is not None:
                    yield conv
            return

        hist = root / "history.jsonl"
        yield from self._iter_history_file(hist, since_mtime, cutoff_ts)
        for session_file in self._iter_session_files(root):
            conv = self._read_session_file(session_file, since_mtime, cutoff_ts)
            if conv is not None:
                yield conv

    def _iter_history_file(
        self, hist: Path, since_mtime: float, cutoff_ts: float
    ) -> Iterable[Conversation]:
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
            if since_mtime and max_ts and max_ts <= since_mtime:
                continue
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

    def _iter_session_files(self, root: Path) -> list[Path]:
        roots: list[Path] = []
        if root.name in _SESSION_DIRS:
            roots.append(root)
        else:
            roots.extend(candidate for name in _SESSION_DIRS if (candidate := root / name).is_dir())
        files: list[Path] = []
        for session_root in roots:
            try:
                files.extend(path for path in session_root.rglob("*.jsonl") if path.is_file())
            except OSError:
                continue
        return sorted(files, key=lambda path: (_safe_mtime(path), str(path)))

    def _read_session_file(
        self, transcript: Path, since_mtime: float, cutoff_ts: float
    ) -> Conversation | None:
        file_mtime = _safe_mtime(transcript)
        if file_mtime and file_mtime <= since_mtime:
            return None

        session_id = transcript.stem
        project_hint = None
        turns: list[str] = []
        fallback_turns: list[str] = []
        max_ts = 0.0

        try:
            with transcript.open("r", encoding="utf-8") as handle:
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
                    payload = obj.get("payload") or {}
                    if not isinstance(payload, dict):
                        continue

                    if obj.get("type") == "session_meta":
                        session_id = str(payload.get("id") or session_id).strip() or session_id
                        cwd = str(payload.get("cwd") or "").strip()
                        if cwd:
                            project_hint = Path(cwd).name or None
                        continue

                    ts = _iso_to_ts(str(obj.get("timestamp") or ""))
                    if obj.get("type") == "event_msg" and payload.get("type") == "user_message":
                        text = str(payload.get("message") or "").strip()
                        if text:
                            turns.append(text)
                            max_ts = max(max_ts, ts)
                        continue

                    if (
                        obj.get("type") == "response_item"
                        and payload.get("type") == "message"
                        and payload.get("role") == "user"
                    ):
                        texts = _content_texts(payload.get("content"))
                        if texts:
                            fallback_turns.extend(texts)
                            max_ts = max(max_ts, ts)
        except OSError:
            return None

        if not turns:
            turns = fallback_turns
        occurred_at = max_ts or file_mtime
        if not turns:
            return None
        if since_mtime and occurred_at and occurred_at <= since_mtime:
            return None
        if cutoff_ts and occurred_at and occurred_at < cutoff_ts:
            return None
        return Conversation(
            agent=self.agent,
            session_id=session_id,
            occurred_at=occurred_at,
            user_turns=turns,
            project_hint=project_hint,
            native_path=str(transcript),
        )
