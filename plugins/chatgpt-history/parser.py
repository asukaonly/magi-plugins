"""Deterministic parser for ChatGPT data exports."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, TextIO


_CONVERSATIONS_MEMBER_RE = re.compile(
    r"^conversations(?:[-_]\d+)?\.json$",
    re.IGNORECASE,
)
_MAX_JSON_BYTES = 128 * 1024 * 1024
_MAX_TOTAL_JSON_BYTES = 192 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_CONVERSATION_DOCUMENTS = 100
_MAX_CONVERSATIONS = 100_000
_MAX_CONVERSATION_JSON_CHARS = 16 * 1024 * 1024
_MAX_MAPPING_NODES = 20_000
_MAX_MESSAGES_TOTAL = 100_000
_MAX_SOURCE_ID_LENGTH = 512
_MAX_MESSAGE_ID_LENGTH = 512
_MAX_SPEAKER_ID_LENGTH = 512
_MAX_SPEAKER_NAME_LENGTH = 256
_MAX_RESULT_WARNINGS = 200
_MAX_SOURCE_WARNINGS = 100
_JSON_READ_CHARS = 1024 * 1024
_SUPPORTED_CONTENT_TYPES = frozenset({"text", "multimodal_text"})


class ChatGPTArchiveError(ValueError):
    """Raised when a selected file is not a supported ChatGPT export."""


@dataclass(frozen=True, slots=True)
class ChatGPTImportWarning:
    """One non-fatal issue discovered while parsing an export."""

    code: str
    source_name: str
    session_id: str | None = None
    message_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ChatGPTMessage:
    """One message on the active branch of a ChatGPT conversation."""

    message_id: str
    message_key: str
    parent_message_id: str | None
    source_order: int
    speaker_id: str
    speaker_name: str
    role_hint: str
    occurred_at: float | None
    content: str
    content_type: str


@dataclass(frozen=True, slots=True)
class ChatGPTSession:
    """One ChatGPT conversation linearized from ``current_node`` ancestry."""

    session_id: str
    source_key: str
    title: str
    created_at: float | None
    updated_at: float | None
    messages: tuple[ChatGPTMessage, ...]
    source_name: str
    warnings: tuple[ChatGPTImportWarning, ...] = ()


@dataclass(slots=True)
class ChatGPTArchiveResult:
    """Normalized sessions and warnings from one selected export."""

    source_name: str
    sessions: list[ChatGPTSession] = field(default_factory=list)
    warnings: list[ChatGPTImportWarning] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ChatGPTArchiveItem:
    """One incrementally parsed session or set of non-fatal warnings."""

    session: ChatGPTSession | None = None
    warnings: tuple[ChatGPTImportWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class _ConversationEntry:
    source_name: str
    source_index: int
    payload: Any


@dataclass(slots=True)
class _WarningAccumulator:
    """Keep warning collection bounded while preserving an overflow signal."""

    limit: int
    source_name: str
    session_id: str | None = None
    items: list[ChatGPTImportWarning] = field(default_factory=list)
    truncated: bool = False

    def add(self, warning: ChatGPTImportWarning) -> None:
        if self.truncated:
            return
        if len(self.items) < self.limit:
            self.items.append(warning)
            return
        self.truncated = True
        marker = ChatGPTImportWarning(
            code="warnings_truncated",
            source_name=self.source_name,
            session_id=self.session_id,
        )
        if self.items:
            self.items[-1] = marker
        elif self.limit > 0:
            self.items.append(marker)

    def extend(self, warnings: Iterable[ChatGPTImportWarning]) -> None:
        for warning in warnings:
            self.add(warning)


def parse_chatgpt_export(path: str | Path) -> ChatGPTArchiveResult:
    """Parse a ChatGPT export ZIP or a selected ``conversations.json`` file.

    The parser only follows explicit archive structure. It never asks an LLM to
    infer speakers, message order, missing branches, or timestamps.

    Args:
        path: Path to an official ChatGPT export ZIP or conversations JSON file.

    Returns:
        Parsed active-branch sessions plus structured, non-fatal warnings.

    Raises:
        ChatGPTArchiveError: If the input cannot be safely recognized or read.
    """

    selected_path = Path(path).expanduser()
    result = ChatGPTArchiveResult(source_name=selected_path.name)
    result_warnings = _WarningAccumulator(
        limit=_MAX_RESULT_WARNINGS,
        source_name=selected_path.name,
    )

    for item in iter_chatgpt_export(selected_path):
        result_warnings.extend(item.warnings)
        if item.session is not None:
            result.sessions.append(item.session)

    result.warnings = result_warnings.items
    return result


def iter_chatgpt_export(path: str | Path) -> Iterable[ChatGPTArchiveItem]:
    """Yield normalized conversations without retaining the complete export."""

    selected_path = Path(path).expanduser()
    seen_session_digests: dict[str, str] = {}
    conversation_count = 0
    message_count = 0

    for entry in _iter_conversations(selected_path):
        conversation_count += 1
        if conversation_count > _MAX_CONVERSATIONS:
            raise ChatGPTArchiveError("ChatGPT export contains too many conversations")
        raw_conversation = entry.payload
        if not isinstance(raw_conversation, dict):
            yield ChatGPTArchiveItem(
                warnings=(
                    ChatGPTImportWarning(
                        code="invalid_conversation",
                        source_name=entry.source_name,
                        detail=f"index={entry.source_index}",
                    ),
                )
            )
            continue

        session, warnings = _parse_conversation(
            raw_conversation,
            source_name=entry.source_name,
            source_index=entry.source_index,
        )
        if session is None:
            yield ChatGPTArchiveItem(warnings=tuple(warnings))
            continue
        session_digest = session_message_digest(session)
        existing_digest = seen_session_digests.get(session.session_id)
        if existing_digest is not None:
            if existing_digest != session_digest:
                raise ChatGPTArchiveError(
                    "ChatGPT export contains conflicting versions of one conversation"
                )
            yield ChatGPTArchiveItem(
                warnings=(
                    ChatGPTImportWarning(
                        code="duplicate_session",
                        source_name=entry.source_name,
                        session_id=session.session_id,
                    ),
                )
            )
            continue
        message_count += len(session.messages)
        if message_count > _MAX_MESSAGES_TOTAL:
            raise ChatGPTArchiveError("ChatGPT export contains too many messages")
        seen_session_digests[session.session_id] = session_digest
        yield ChatGPTArchiveItem(session=session)


def _iter_conversations(path: Path) -> Iterable[_ConversationEntry]:
    """Yield top-level conversations without retaining complete JSON documents."""

    if not path.is_file():
        raise ChatGPTArchiveError("Selected ChatGPT export is not a file")

    suffix = path.suffix.casefold()
    if suffix == ".json":
        if path.stat().st_size > _MAX_JSON_BYTES:
            raise ChatGPTArchiveError("ChatGPT conversations JSON is too large")
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                for index, payload in enumerate(
                    _iter_json_array(handle, source_name=path.name)
                ):
                    yield _ConversationEntry(
                        source_name=path.name,
                        source_index=index,
                        payload=payload,
                    )
            return
        except ChatGPTArchiveError:
            raise
        except (OSError, UnicodeDecodeError) as exc:
            raise ChatGPTArchiveError(
                "Unable to read ChatGPT conversations JSON"
            ) from exc

    if suffix != ".zip":
        raise ChatGPTArchiveError(
            "Select a ChatGPT export ZIP or conversations JSON file"
        )

    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > _MAX_ARCHIVE_MEMBERS:
                raise ChatGPTArchiveError("ChatGPT export contains too many files")
            selected = sorted(
                (
                    member
                    for member in members
                    if not member.is_dir()
                    and _CONVERSATIONS_MEMBER_RE.fullmatch(Path(member.filename).name)
                ),
                key=lambda member: member.filename.casefold(),
            )
            if not selected:
                raise ChatGPTArchiveError(
                    "ChatGPT export does not contain conversations.json"
                )
            if len(selected) > _MAX_CONVERSATION_DOCUMENTS:
                raise ChatGPTArchiveError(
                    "ChatGPT export contains too many conversations JSON files"
                )

            selected_names = [
                Path(member.filename).name.casefold() for member in selected
            ]
            if len(selected_names) != len(set(selected_names)):
                raise ChatGPTArchiveError(
                    "ChatGPT export contains duplicate conversations JSON files"
                )

            total_json_bytes = 0
            for member in selected:
                if member.flag_bits & 0x1:
                    raise ChatGPTArchiveError(
                        "Encrypted ChatGPT exports are not supported"
                    )
                compression_name = Path(member.filename).name.casefold()
                if member.compress_size == 0 and member.file_size > 0:
                    raise ChatGPTArchiveError("ChatGPT export has invalid ZIP metadata")
                if (
                    member.compress_size > 0
                    and member.file_size > 16 * 1024 * 1024
                    and member.file_size / member.compress_size > 100
                ):
                    raise ChatGPTArchiveError(
                        f"ChatGPT export entry is suspiciously compressed: {compression_name}"
                    )
                if member.file_size > _MAX_JSON_BYTES:
                    raise ChatGPTArchiveError("ChatGPT conversations JSON is too large")
                total_json_bytes += member.file_size
                if total_json_bytes > _MAX_TOTAL_JSON_BYTES:
                    raise ChatGPTArchiveError("ChatGPT conversations JSON is too large")
                try:
                    with archive.open(member, "r") as raw_handle:
                        with io.TextIOWrapper(
                            raw_handle,
                            encoding="utf-8-sig",
                        ) as text_handle:
                            for index, payload in enumerate(
                                _iter_json_array(
                                    text_handle,
                                    source_name=member.filename,
                                )
                            ):
                                yield _ConversationEntry(
                                    source_name=member.filename,
                                    source_index=index,
                                    payload=payload,
                                )
                except ChatGPTArchiveError:
                    raise
                except (
                    OSError,
                    RuntimeError,
                    UnicodeDecodeError,
                    zipfile.BadZipFile,
                ) as exc:
                    raise ChatGPTArchiveError(
                        "Unable to read ChatGPT conversations from the export"
                    ) from exc
    except ChatGPTArchiveError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise ChatGPTArchiveError(
            "Selected file is not a valid ChatGPT export ZIP"
        ) from exc


def _iter_json_array(handle: TextIO, *, source_name: str) -> Iterable[Any]:
    """Decode one top-level JSON array while bounding each retained value."""

    decoder = json.JSONDecoder()
    buffer = ""
    position = 0
    state = "start"
    reached_eof = False

    def read_more() -> None:
        nonlocal buffer, reached_eof
        chunk = handle.read(_JSON_READ_CHARS)
        if chunk:
            buffer += chunk
        else:
            reached_eof = True

    def skip_whitespace() -> None:
        nonlocal position
        while position < len(buffer) and buffer[position].isspace():
            position += 1

    while True:
        skip_whitespace()
        if position >= len(buffer) and not reached_eof:
            buffer = ""
            position = 0
            read_more()
            continue

        if state == "start":
            if reached_eof and position >= len(buffer):
                raise ChatGPTArchiveError(
                    f"Invalid ChatGPT conversations JSON: {source_name}"
                )
            if buffer[position] != "[":
                raise ChatGPTArchiveError(
                    "ChatGPT conversations JSON must contain a list: " f"{source_name}"
                )
            position += 1
            state = "value_or_end"
            continue

        if state in {"value_or_end", "value"}:
            if reached_eof and position >= len(buffer):
                raise ChatGPTArchiveError(
                    f"Invalid ChatGPT conversations JSON: {source_name}"
                )
            if position >= len(buffer):
                read_more()
                continue
            if buffer[position] == "]":
                if state == "value":
                    raise ChatGPTArchiveError(
                        f"Invalid ChatGPT conversations JSON: {source_name}"
                    )
                position += 1
                state = "finished"
                continue

            if position:
                buffer = buffer[position:]
                position = 0
            try:
                payload, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError as exc:
                if len(buffer) > _MAX_CONVERSATION_JSON_CHARS:
                    raise ChatGPTArchiveError(
                        "ChatGPT export contains an oversized conversation"
                    ) from exc
                if reached_eof:
                    raise ChatGPTArchiveError(
                        f"Invalid ChatGPT conversations JSON: {source_name}"
                    ) from exc
                read_more()
                continue
            if end > _MAX_CONVERSATION_JSON_CHARS:
                raise ChatGPTArchiveError(
                    "ChatGPT export contains an oversized conversation"
                )
            position = end
            state = "delimiter"
            yield payload
            continue

        if state == "delimiter":
            if reached_eof and position >= len(buffer):
                raise ChatGPTArchiveError(
                    f"Invalid ChatGPT conversations JSON: {source_name}"
                )
            if position >= len(buffer):
                buffer = ""
                position = 0
                read_more()
                continue
            token = buffer[position]
            position += 1
            if token == ",":
                state = "value"
                continue
            if token == "]":
                state = "finished"
                continue
            raise ChatGPTArchiveError(
                f"Invalid ChatGPT conversations JSON: {source_name}"
            )

        if state == "finished":
            if any(not character.isspace() for character in buffer[position:]):
                raise ChatGPTArchiveError(
                    f"Invalid ChatGPT conversations JSON: {source_name}"
                )
            if reached_eof:
                return
            buffer = ""
            position = 0
            read_more()


def _parse_conversation(
    conversation: dict[str, Any],
    *,
    source_name: str,
    source_index: int,
) -> tuple[ChatGPTSession | None, list[ChatGPTImportWarning]]:
    mapping = conversation.get("mapping")
    current_node = _optional_string(conversation.get("current_node"))
    session_id, derived_id = _session_id(
        conversation,
        source_name=source_name,
        source_index=source_index,
    )
    if len(session_id) > _MAX_SOURCE_ID_LENGTH:
        raise ChatGPTArchiveError("ChatGPT conversation identifier is too long")
    warnings = _WarningAccumulator(
        limit=_MAX_SOURCE_WARNINGS,
        source_name=source_name,
        session_id=session_id,
    )
    if derived_id:
        warnings.add(
            ChatGPTImportWarning(
                code="derived_session_id",
                source_name=source_name,
                session_id=session_id,
            )
        )

    if not isinstance(mapping, dict):
        warnings.add(
            ChatGPTImportWarning(
                code="invalid_mapping",
                source_name=source_name,
                session_id=session_id,
            )
        )
        return None, warnings.items
    if len(mapping) > _MAX_MAPPING_NODES:
        raise ChatGPTArchiveError("ChatGPT conversation contains too many messages")
    if not current_node:
        warnings.add(
            ChatGPTImportWarning(
                code="missing_current_node",
                source_name=source_name,
                session_id=session_id,
            )
        )
        return None, warnings.items
    if current_node not in mapping:
        warnings.add(
            ChatGPTImportWarning(
                code="unknown_current_node",
                source_name=source_name,
                session_id=session_id,
                detail=current_node,
            )
        )
        return None, warnings.items

    ancestry, ancestry_warnings = _active_ancestry(
        mapping,
        current_node=current_node,
        source_name=source_name,
        session_id=session_id,
    )
    warnings.extend(ancestry_warnings)
    if not ancestry:
        return None, warnings.items

    messages: list[ChatGPTMessage] = []
    for source_order, node_id in enumerate(ancestry):
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            warnings.add(
                ChatGPTImportWarning(
                    code="invalid_node",
                    source_name=source_name,
                    session_id=session_id,
                    message_id=node_id,
                )
            )
            continue
        raw_message = node.get("message")
        if raw_message is None:
            warnings.add(
                ChatGPTImportWarning(
                    code="null_message",
                    source_name=source_name,
                    session_id=session_id,
                    message_id=node_id,
                )
            )
            continue
        if not isinstance(raw_message, dict):
            warnings.add(
                ChatGPTImportWarning(
                    code="invalid_message",
                    source_name=source_name,
                    session_id=session_id,
                    message_id=node_id,
                )
            )
            continue

        parsed, message_warnings = _parse_message(
            raw_message,
            node=node,
            node_id=node_id,
            mapping=mapping,
            source_order=source_order,
            source_name=source_name,
            session_id=session_id,
        )
        warnings.extend(message_warnings)
        if parsed is not None:
            messages.append(parsed)

    if not messages:
        warnings.add(
            ChatGPTImportWarning(
                code="empty_session",
                source_name=source_name,
                session_id=session_id,
            )
        )
        return None, warnings.items

    return (
        ChatGPTSession(
            session_id=session_id,
            source_key=session_id,
            title=_optional_string(conversation.get("title")) or "",
            created_at=_timestamp(conversation.get("create_time")),
            updated_at=_timestamp(conversation.get("update_time")),
            messages=tuple(messages),
            source_name=source_name,
            warnings=tuple(warnings.items),
        ),
        warnings.items,
    )


def _active_ancestry(
    mapping: dict[str, Any],
    *,
    current_node: str,
    source_name: str,
    session_id: str,
) -> tuple[list[str], list[ChatGPTImportWarning]]:
    warnings = _WarningAccumulator(
        limit=_MAX_SOURCE_WARNINGS,
        source_name=source_name,
        session_id=session_id,
    )
    reverse_path: list[str] = []
    visited: set[str] = set()
    node_id: str | None = current_node

    while node_id:
        if node_id in visited:
            warnings.add(
                ChatGPTImportWarning(
                    code="cyclic_ancestry",
                    source_name=source_name,
                    session_id=session_id,
                    message_id=node_id,
                )
            )
            return [], warnings.items
        visited.add(node_id)
        raw_node = mapping.get(node_id)
        if not isinstance(raw_node, dict):
            warnings.add(
                ChatGPTImportWarning(
                    code="missing_parent_node",
                    source_name=source_name,
                    session_id=session_id,
                    message_id=node_id,
                )
            )
            return [], warnings.items
        reverse_path.append(node_id)
        raw_parent = raw_node.get("parent")
        if raw_parent is None:
            node_id = None
        elif isinstance(raw_parent, str) and raw_parent:
            node_id = raw_parent
        else:
            warnings.add(
                ChatGPTImportWarning(
                    code="invalid_parent_id",
                    source_name=source_name,
                    session_id=session_id,
                    message_id=node_id,
                )
            )
            return [], warnings.items

    reverse_path.reverse()
    return reverse_path, warnings.items


def _parse_message(
    raw_message: dict[str, Any],
    *,
    node: dict[str, Any],
    node_id: str,
    mapping: dict[str, Any],
    source_order: int,
    source_name: str,
    session_id: str,
) -> tuple[ChatGPTMessage | None, list[ChatGPTImportWarning]]:
    warnings = _WarningAccumulator(
        limit=_MAX_SOURCE_WARNINGS,
        source_name=source_name,
        session_id=session_id,
    )
    message_id = _optional_string(raw_message.get("id")) or node_id
    if len(message_id) > _MAX_MESSAGE_ID_LENGTH:
        raise ChatGPTArchiveError("ChatGPT message identifier is too long")
    content = raw_message.get("content")
    if not isinstance(content, dict):
        warnings.add(
            ChatGPTImportWarning(
                code="invalid_content",
                source_name=source_name,
                session_id=session_id,
                message_id=message_id,
            )
        )
        return None, warnings.items

    content_type = _optional_string(content.get("content_type")) or ""
    if content_type not in _SUPPORTED_CONTENT_TYPES:
        warnings.add(
            ChatGPTImportWarning(
                code="unsupported_content_type",
                source_name=source_name,
                session_id=session_id,
                message_id=message_id,
                detail=content_type or "missing",
            )
        )
        return None, warnings.items

    parts = content.get("parts")
    if not isinstance(parts, list):
        warnings.add(
            ChatGPTImportWarning(
                code="invalid_content_parts",
                source_name=source_name,
                session_id=session_id,
                message_id=message_id,
            )
        )
        return None, warnings.items

    text_parts = [part for part in parts if isinstance(part, str) and part.strip()]
    unsupported_part_count = sum(1 for part in parts if not isinstance(part, str))
    if unsupported_part_count:
        warnings.add(
            ChatGPTImportWarning(
                code="unsupported_content_part",
                source_name=source_name,
                session_id=session_id,
                message_id=message_id,
                detail=str(unsupported_part_count),
            )
        )
    normalized_content = "\n\n".join(text_parts).strip()
    if not normalized_content:
        warnings.add(
            ChatGPTImportWarning(
                code="empty_text_content",
                source_name=source_name,
                session_id=session_id,
                message_id=message_id,
            )
        )
        return None, warnings.items

    author = raw_message.get("author")
    role_hint = "unknown"
    author_name = ""
    if isinstance(author, dict):
        role_hint = (_optional_string(author.get("role")) or "unknown").casefold()
        author_name = _optional_string(author.get("name")) or ""
    else:
        warnings.add(
            ChatGPTImportWarning(
                code="missing_author",
                source_name=source_name,
                session_id=session_id,
                message_id=message_id,
            )
        )

    speaker_id = (
        role_hint
        if role_hint in {"user", "assistant", "system", "tool"}
        else author_name or role_hint
    )
    speaker_name = author_name or _default_speaker_name(role_hint)
    if len(speaker_id) > _MAX_SPEAKER_ID_LENGTH:
        raise ChatGPTArchiveError("ChatGPT speaker identifier is too long")
    if len(speaker_name) > _MAX_SPEAKER_NAME_LENGTH:
        raise ChatGPTArchiveError("ChatGPT speaker name is too long")
    parent_message_id = _parent_message_id(node, mapping)
    occurred_at = _timestamp(raw_message.get("create_time"))
    if occurred_at is None:
        warnings.add(
            ChatGPTImportWarning(
                code="missing_message_timestamp",
                source_name=source_name,
                session_id=session_id,
                message_id=message_id,
            )
        )

    return (
        ChatGPTMessage(
            message_id=message_id,
            message_key=message_id,
            parent_message_id=parent_message_id,
            source_order=source_order,
            speaker_id=speaker_id,
            speaker_name=speaker_name,
            role_hint=role_hint,
            occurred_at=occurred_at,
            content=normalized_content,
            content_type=content_type,
        ),
        warnings.items,
    )


def _parent_message_id(node: dict[str, Any], mapping: dict[str, Any]) -> str | None:
    parent_node_id = _optional_string(node.get("parent"))
    if not parent_node_id:
        return None
    parent_node = mapping.get(parent_node_id)
    if not isinstance(parent_node, dict):
        return parent_node_id
    parent_message = parent_node.get("message")
    if isinstance(parent_message, dict):
        return _optional_string(parent_message.get("id")) or parent_node_id
    return None


def _session_id(
    conversation: dict[str, Any],
    *,
    source_name: str,
    source_index: int,
) -> tuple[str, bool]:
    explicit = _optional_string(conversation.get("id")) or _optional_string(
        conversation.get("conversation_id")
    )
    if explicit:
        return explicit, False
    mapping = conversation.get("mapping")
    root_node_ids: Iterable[str] = ()
    if isinstance(mapping, dict):
        root_node_ids = sorted(
            str(key)
            for key, node in mapping.items()
            if isinstance(node, dict) and node.get("parent") is None
        )
    identity = json.dumps(
        {
            "create_time": _timestamp(conversation.get("create_time")),
            "root_node_ids": list(root_node_ids),
            "fallback_source_name": source_name if not root_node_ids else None,
            "fallback_source_index": source_index if not root_node_ids else None,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"derived_{digest}", True


def _default_speaker_name(role_hint: str) -> str:
    return {
        "user": "User",
        "assistant": "ChatGPT",
        "system": "System",
        "tool": "Tool",
    }.get(role_hint, role_hint or "Unknown")


def _timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(timestamp) or timestamp < 0:
        return None
    return timestamp


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def sessions_have_same_messages(
    left: ChatGPTSession,
    right: ChatGPTSession,
) -> bool:
    """Compare normalized source-declared message identity and content."""

    return session_message_digest(left) == session_message_digest(right)


def session_message_digest(session: ChatGPTSession) -> str:
    """Hash normalized message identity without retaining another session copy."""

    digest = hashlib.sha256()
    for message in session.messages:
        signature = json.dumps(
            (
                message.message_key,
                message.parent_message_id,
                message.source_order,
                message.speaker_id,
                message.role_hint,
                message.occurred_at,
                message.content,
                message.content_type,
            ),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(signature).to_bytes(8, "big"))
        digest.update(signature)
    return digest.hexdigest()


__all__ = [
    "ChatGPTArchiveError",
    "ChatGPTArchiveItem",
    "ChatGPTArchiveResult",
    "ChatGPTImportWarning",
    "ChatGPTMessage",
    "ChatGPTSession",
    "iter_chatgpt_export",
    "parse_chatgpt_export",
    "session_message_digest",
    "sessions_have_same_messages",
]
