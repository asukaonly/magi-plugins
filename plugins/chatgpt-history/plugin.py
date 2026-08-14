"""ChatGPT data export history importer plugin."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from magi_plugin_sdk import Plugin
from magi_plugin_sdk.history_imports import (
    HistoryImporter,
    HistoryImporterSpec,
    HistoryImportParseResult,
    HistoryImportRecord,
    HistoryImportSource,
    MAX_HISTORY_IMPORT_SOURCES,
)

from .parser import (
    ChatGPTArchiveError,
    ChatGPTImportWarning,
    ChatGPTSession,
    iter_chatgpt_export,
    session_message_digest,
)


_IMPORTER_ID = "chatgpt_export"
_FORMAT_VERSION = "chatgpt-export-v1"
_MAX_RESULT_WARNINGS = 200
_MAX_SOURCE_WARNINGS = 100
_MAX_WARNING_TEXT_LENGTH = 512
_MAX_SOURCE_NAME_LENGTH = 512
_MAX_RECORDS_PER_SOURCE = 20_000
_MAX_CONTENT_LENGTH = 1_000_000
_MAX_TOTAL_RECORDS = 100_000
_MAX_TOTAL_CONTENT_CHARS = 50_000_000
_MAX_TOTAL_SOURCES = MAX_HISTORY_IMPORT_SOURCES
_TRUNCATED_WARNING = "warnings_truncated"
_EXPORT_HELP_URL = (
    "https://help.openai.com/en/articles/"
    "7260999-how-do-i-export-my-chatgpt-history-and-data"
)


class ChatGPTHistoryImporter(HistoryImporter):
    """Adapt official ChatGPT exports into normalized conversation sources."""

    def parse(self, paths: list[Path]) -> HistoryImportParseResult:
        sources: list[HistoryImportSource] = []
        warnings = _StringWarningAccumulator(limit=_MAX_RESULT_WARNINGS)
        seen_source_digests: dict[str, str] = {}
        total_records = 0
        total_content_chars = 0

        for selected_path in paths:
            for item in iter_chatgpt_export(selected_path):
                warnings.extend(_warning_text(entry) for entry in item.warnings)
                session = item.session
                if session is None:
                    continue
                session_digest = session_message_digest(session)
                existing_digest = seen_source_digests.get(session.source_key)
                if existing_digest is not None:
                    if existing_digest != session_digest:
                        raise ChatGPTArchiveError(
                            "Selected ChatGPT exports contain conflicting versions "
                            "of one conversation"
                        )
                    warnings.add(
                        _bounded_warning_fields(
                            "duplicate_session",
                            session.source_key,
                            selected_path.name,
                        )
                    )
                    continue
                if len(sources) >= _MAX_TOTAL_SOURCES:
                    raise ChatGPTArchiveError(
                        "Selected ChatGPT export contains too many conversations"
                    )
                if len(session.messages) > _MAX_RECORDS_PER_SOURCE:
                    raise ChatGPTArchiveError(
                        "ChatGPT conversation contains too many messages"
                    )
                if any(
                    len(message.content) > _MAX_CONTENT_LENGTH
                    for message in session.messages
                ):
                    raise ChatGPTArchiveError(
                        "ChatGPT conversation contains an oversized message"
                    )
                session_content_chars = sum(
                    len(message.content) for message in session.messages
                )
                if total_records + len(session.messages) > _MAX_TOTAL_RECORDS:
                    raise ChatGPTArchiveError(
                        "Selected ChatGPT exports contain too many messages"
                    )
                if (
                    total_content_chars + session_content_chars
                    > _MAX_TOTAL_CONTENT_CHARS
                ):
                    raise ChatGPTArchiveError(
                        "Selected ChatGPT exports contain too much message text"
                    )
                total_records += len(session.messages)
                total_content_chars += session_content_chars
                seen_source_digests[session.source_key] = session_digest
                session_warnings = _StringWarningAccumulator(limit=_MAX_SOURCE_WARNINGS)
                session_warnings.extend(
                    _warning_text(item) for item in session.warnings
                )
                sources.append(
                    _source_from_session(
                        session,
                        warnings=session_warnings.items,
                    )
                )

        if not sources:
            raise ChatGPTArchiveError(
                "The selected ChatGPT export contains no supported text conversations"
            )
        return HistoryImportParseResult(sources=sources, warnings=warnings.items)


class ChatGPTHistoryPlugin(Plugin):
    """Register an importer for official ChatGPT data exports."""

    def get_history_importers(
        self,
    ) -> list[tuple[str, HistoryImporter, HistoryImporterSpec]]:
        spec = HistoryImporterSpec(
            importer_id=_IMPORTER_ID,
            display_name="ChatGPT history",
            display_name_i18n={
                "en": self.t(
                    "chatgpt_history.importer.display_name",
                    language="en",
                    fallback="ChatGPT history",
                ),
                "zh-CN": self.t(
                    "chatgpt_history.importer.display_name",
                    language="zh-CN",
                    fallback="ChatGPT 历史",
                ),
            },
            description=(
                "Import conversations from an official ChatGPT data export. "
                "This is a one-time local import and does not connect to your account."
            ),
            description_i18n={
                "en": self.t(
                    "chatgpt_history.importer.description",
                    language="en",
                    fallback=(
                        "Import conversations from an official ChatGPT data export. "
                        "This is a one-time local import and does not connect to your account."
                    ),
                ),
                "zh-CN": self.t(
                    "chatgpt_history.importer.description",
                    language="zh-CN",
                    fallback=(
                        "从 ChatGPT 官方数据导出文件中一次性导入对话；"
                        "所有解析均在本机完成，不会连接你的账户。"
                    ),
                ),
            },
            accepted_extensions=[".zip", ".json"],
            format_version=_FORMAT_VERSION,
            participant_identity_scope="export",
            export_help_url=_EXPORT_HELP_URL,
        )
        return [(_IMPORTER_ID, ChatGPTHistoryImporter(), spec)]


def _source_from_session(
    session: ChatGPTSession,
    *,
    warnings: list[str],
) -> HistoryImportSource:
    known_message_keys = {message.message_key for message in session.messages}
    return HistoryImportSource(
        source_id=session.source_key,
        source_name=_bounded_source_name(session.title),
        session_key=session.session_id,
        detected_kind="chat",
        warnings=warnings,
        records=[
            HistoryImportRecord(
                message_key=message.message_key,
                source_order=message.source_order,
                speaker_id=message.speaker_id,
                speaker_name=message.speaker_name,
                role_hint=_normalized_role_hint(message.role_hint),
                content=message.content,
                occurred_at=message.occurred_at,
                timestamp_confidence=(
                    "exact" if message.occurred_at is not None else "unknown"
                ),
                parent_message_key=(
                    message.parent_message_id
                    if message.parent_message_id in known_message_keys
                    else None
                ),
            )
            for message in session.messages
        ],
    )


def _bounded_source_name(title: str) -> str:
    normalized = title.strip() or "ChatGPT conversation"
    if len(normalized) <= _MAX_SOURCE_NAME_LENGTH:
        return normalized
    return normalized[: _MAX_SOURCE_NAME_LENGTH - 1].rstrip() + "…"


def _normalized_role_hint(role_hint: str) -> str:
    if role_hint in {"user", "assistant"}:
        return role_hint
    if role_hint == "unknown":
        return "unknown"
    return "other"


def _warning_text(warning: ChatGPTImportWarning) -> str:
    if warning.code == _TRUNCATED_WARNING:
        return _TRUNCATED_WARNING
    fields = [warning.code, warning.source_name]
    if warning.session_id:
        fields.append(warning.session_id)
    if warning.message_id:
        fields.append(warning.message_id)
    if warning.detail:
        fields.append(warning.detail)
    return _bounded_warning_fields(*fields)


class _StringWarningAccumulator:
    """Bound normalized SDK warnings without accumulating overflow entries."""

    def __init__(self, *, limit: int) -> None:
        self.limit = limit
        self.items: list[str] = []
        self._truncated = False

    def add(self, warning: str) -> None:
        if self._truncated:
            return
        normalized = _bounded_warning_text(warning)
        if len(self.items) < self.limit:
            self.items.append(normalized)
            return
        self._truncated = True
        if self.items:
            self.items[-1] = _TRUNCATED_WARNING
        elif self.limit > 0:
            self.items.append(_TRUNCATED_WARNING)

    def extend(self, warnings: Iterable[str]) -> None:
        for warning in warnings:
            self.add(warning)


def _bounded_warning_text(warning: str) -> str:
    normalized = str(warning)
    if len(normalized) <= _MAX_WARNING_TEXT_LENGTH:
        return normalized
    suffix = ":text_truncated"
    return normalized[: _MAX_WARNING_TEXT_LENGTH - len(suffix)] + suffix


def _bounded_warning_fields(*fields: str) -> str:
    bounded_fields = [str(field)[:_MAX_WARNING_TEXT_LENGTH] for field in fields]
    return _bounded_warning_text(":".join(bounded_fields))


__all__ = ["ChatGPTHistoryImporter", "ChatGPTHistoryPlugin"]
