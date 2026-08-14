from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from chatgpt_history_under_test import parser as parser_module
from chatgpt_history_under_test.parser import (
    ChatGPTArchiveError,
    iter_chatgpt_export,
    parse_chatgpt_export,
)


FIXTURE = Path(__file__).parent / "fixtures" / "conversations.json"


def _fixture_payload() -> list[dict[str, object]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_parses_only_active_current_node_ancestry() -> None:
    result = parse_chatgpt_export(FIXTURE)

    assert len(result.sessions) == 1
    session = result.sessions[0]
    assert session.session_id == "conversation-stable-001"
    assert session.source_key == "conversation-stable-001"
    assert [message.message_id for message in session.messages] == [
        "message-user-1",
        "message-assistant-active",
        "message-user-2",
    ]
    assert "message-assistant-unused" not in {
        message.message_id for message in session.messages
    }
    assert [message.source_order for message in session.messages] == [1, 2, 3]
    assert [message.role_hint for message in session.messages] == [
        "user",
        "assistant",
        "user",
    ]
    assert [message.speaker_id for message in session.messages] == [
        "user",
        "assistant",
        "user",
    ]
    assert session.messages[1].speaker_name == "ChatGPT"
    assert session.messages[0].parent_message_id is None
    assert session.messages[1].parent_message_id == "message-user-1"
    assert session.messages[2].parent_message_id == "message-assistant-active"
    assert session.messages[2].content == "[REDACTED USER TEXT 2]"
    assert result.warnings == []
    assert {warning.code for warning in session.warnings} == {
        "null_message",
        "unsupported_content_part",
    }


def test_parses_official_zip_without_extracting_files(tmp_path: Path) -> None:
    archive_path = tmp_path / "chatgpt-export.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("export/conversations.json", FIXTURE.read_bytes())
        archive.writestr("export/chat.html", "ignored")

    result = parse_chatgpt_export(archive_path)

    assert [session.session_id for session in result.sessions] == [
        "conversation-stable-001"
    ]
    assert result.sessions[0].source_name == "export/conversations.json"


def test_json_input_is_streamed_without_reading_the_complete_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_read_bytes(path: Path) -> bytes:
        raise AssertionError(f"unexpected whole-file read: {path}")

    monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)

    result = parse_chatgpt_export(FIXTURE)

    assert [session.session_id for session in result.sessions] == [
        "conversation-stable-001"
    ]


def test_incremental_export_stops_parsing_when_consumer_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _fixture_payload()
    second = json.loads(json.dumps(payload[0]))
    second["id"] = "conversation-stable-002"
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps([payload[0], second]), encoding="utf-8")
    original = parser_module._parse_conversation
    calls = 0

    def track_parse(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(parser_module, "_parse_conversation", track_parse)
    stream = iter_chatgpt_export(path)

    first = next(iter(stream))

    assert first.session is not None
    assert first.session.session_id == "conversation-stable-001"
    assert calls == 1
    getattr(stream, "close")()


def test_reads_numbered_conversation_json_members(tmp_path: Path) -> None:
    first = _fixture_payload()
    second = _fixture_payload()
    second[0]["id"] = "conversation-stable-002"
    archive_path = tmp_path / "chatgpt-export.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("conversations-001.json", json.dumps(first))
        archive.writestr("conversations_002.json", json.dumps(second))

    result = parse_chatgpt_export(archive_path)

    assert [session.session_id for session in result.sessions] == [
        "conversation-stable-001",
        "conversation-stable-002",
    ]


def test_identical_conversation_across_numbered_members_is_deduplicated(
    tmp_path: Path,
) -> None:
    payload = _fixture_payload()
    archive_path = tmp_path / "chatgpt-export.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("conversations-001.json", json.dumps(payload))
        archive.writestr("conversations-002.json", json.dumps(payload))

    result = parse_chatgpt_export(archive_path)

    assert [session.session_id for session in result.sessions] == [
        "conversation-stable-001"
    ]
    assert [warning.code for warning in result.warnings] == ["duplicate_session"]


def test_conflicting_conversation_across_numbered_members_fails_closed(
    tmp_path: Path,
) -> None:
    first = _fixture_payload()
    second = _fixture_payload()
    second[0]["mapping"]["node-user-2"]["message"]["content"]["parts"] = [
        "[DIFFERENT TEXT]"
    ]
    archive_path = tmp_path / "chatgpt-export.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("conversations-001.json", json.dumps(first))
        archive.writestr("conversations-002.json", json.dumps(second))

    with pytest.raises(ChatGPTArchiveError, match="conflicting versions"):
        parse_chatgpt_export(archive_path)


def test_many_unrelated_archive_members_do_not_consume_document_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "chatgpt-export.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for index in range(20):
            archive.writestr(f"attachments/item-{index}.txt", "ignored")
        archive.writestr("conversations.json", FIXTURE.read_bytes())
    monkeypatch.setattr(parser_module, "_MAX_CONVERSATION_DOCUMENTS", 1)

    result = parse_chatgpt_export(archive_path)

    assert [session.session_id for session in result.sessions] == [
        "conversation-stable-001"
    ]


def test_rejects_too_many_conversation_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "chatgpt-export.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("conversations-001.json", "[]")
        archive.writestr("conversations-002.json", "[]")
    monkeypatch.setattr(parser_module, "_MAX_CONVERSATION_DOCUMENTS", 1)

    with pytest.raises(ChatGPTArchiveError, match="too many conversations JSON"):
        parse_chatgpt_export(archive_path)


def test_message_keys_survive_incremental_export_growth(tmp_path: Path) -> None:
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_payload = _fixture_payload()
    second_payload = _fixture_payload()
    first_path.write_text(json.dumps(first_payload), encoding="utf-8")

    mapping = second_payload[0]["mapping"]
    assert isinstance(mapping, dict)
    mapping["node-user-3"] = {
        "id": "node-user-3",
        "parent": "node-user-2",
        "children": [],
        "message": {
            "id": "message-user-3",
            "author": {"role": "user", "name": None},
            "create_time": 1710000040,
            "content": {"content_type": "text", "parts": ["[NEW TEXT]"]},
        },
    }
    node_user_2 = mapping["node-user-2"]
    assert isinstance(node_user_2, dict)
    node_user_2["children"] = ["node-user-3"]
    second_payload[0]["current_node"] = "node-user-3"
    second_path.write_text(json.dumps(second_payload), encoding="utf-8")

    first = parse_chatgpt_export(first_path).sessions[0]
    second = parse_chatgpt_export(second_path).sessions[0]

    assert [message.message_key for message in first.messages] == [
        message.message_key for message in second.messages[:3]
    ]
    assert second.messages[-1].message_key == "message-user-3"


def test_derived_session_id_survives_export_growth_and_filename_change(
    tmp_path: Path,
) -> None:
    first_payload = _fixture_payload()
    second_payload = _fixture_payload()
    first_payload[0].pop("id")
    second_payload[0].pop("id")
    mapping = second_payload[0]["mapping"]
    assert isinstance(mapping, dict)
    mapping["node-new"] = {
        "id": "node-new",
        "parent": "node-user-2",
        "children": [],
        "message": {
            "id": "message-new",
            "author": {"role": "user"},
            "create_time": 1710000040,
            "content": {"content_type": "text", "parts": ["[NEW TEXT]"]},
        },
    }
    mapping["node-user-2"]["children"] = ["node-new"]
    second_payload[0]["current_node"] = "node-new"
    first_path = tmp_path / "conversations.json"
    second_path = tmp_path / "renamed-conversations.json"
    first_path.write_text(json.dumps(first_payload), encoding="utf-8")
    second_path.write_text(json.dumps(second_payload), encoding="utf-8")

    first = parse_chatgpt_export(first_path).sessions[0]
    second = parse_chatgpt_export(second_path).sessions[0]

    assert first.session_id == second.session_id


def test_skips_unknown_content_type_with_warning(tmp_path: Path) -> None:
    payload = _fixture_payload()
    mapping = payload[0]["mapping"]
    assert isinstance(mapping, dict)
    user_node = mapping["node-user-1"]
    assert isinstance(user_node, dict)
    message = user_node["message"]
    assert isinstance(message, dict)
    message["content"] = {"content_type": "audio", "transcript": "do not infer"}
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = parse_chatgpt_export(path)

    assert [message.message_id for message in result.sessions[0].messages] == [
        "message-assistant-active",
        "message-user-2",
    ]
    warnings = [
        warning
        for warning in result.sessions[0].warnings
        if warning.code == "unsupported_content_type"
    ]
    assert [(warning.message_id, warning.detail) for warning in warnings] == [
        ("message-user-1", "audio")
    ]


@pytest.mark.parametrize("current_node", [None, "unknown-node"])
def test_does_not_guess_a_branch_when_current_node_is_unusable(
    tmp_path: Path,
    current_node: str | None,
) -> None:
    payload = _fixture_payload()
    payload[0]["current_node"] = current_node
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = parse_chatgpt_export(path)

    assert result.sessions == []
    assert result.warnings[-1].code in {"missing_current_node", "unknown_current_node"}


def test_rejects_non_conversation_json_shape(tmp_path: Path) -> None:
    path = tmp_path / "conversations.json"
    path.write_text('{"conversations": []}', encoding="utf-8")

    with pytest.raises(ChatGPTArchiveError, match="must contain a list"):
        parse_chatgpt_export(path)


def test_rejects_zip_without_conversations_json(tmp_path: Path) -> None:
    archive_path = tmp_path / "not-an-export.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("account.json", "{}")

    with pytest.raises(ChatGPTArchiveError, match="does not contain"):
        parse_chatgpt_export(archive_path)


def test_rejects_oversized_conversations_entry_from_zip_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "oversized.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("conversations.json", "[]")

    monkeypatch.setattr(
        "chatgpt_history_under_test.parser._MAX_JSON_BYTES",
        1,
    )

    with pytest.raises(ChatGPTArchiveError, match="too large"):
        parse_chatgpt_export(archive_path)


def test_rejects_oversized_json_before_opening_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "conversations.json"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(parser_module, "_MAX_JSON_BYTES", 1)

    def reject_open(*args: object, **kwargs: object) -> object:
        raise AssertionError("oversized input should be rejected before opening")

    monkeypatch.setattr(Path, "open", reject_open)

    with pytest.raises(ChatGPTArchiveError, match="too large"):
        parse_chatgpt_export(path)


def test_rejects_one_oversized_conversation_without_truncating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps([{"title": "x" * 100}]), encoding="utf-8")
    monkeypatch.setattr(parser_module, "_MAX_CONVERSATION_JSON_CHARS", 32)
    monkeypatch.setattr(parser_module, "_JSON_READ_CHARS", 8)

    with pytest.raises(ChatGPTArchiveError, match="oversized conversation"):
        parse_chatgpt_export(path)


@pytest.mark.parametrize("raw", ["[null,]", "[null] trailing", "[null null]"])
def test_streaming_decoder_rejects_invalid_array_boundaries(
    tmp_path: Path,
    raw: str,
) -> None:
    path = tmp_path / "conversations.json"
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(ChatGPTArchiveError, match="Invalid ChatGPT conversations JSON"):
        parse_chatgpt_export(path)


def test_rejects_duplicate_conversations_members(tmp_path: Path) -> None:
    archive_path = tmp_path / "duplicate-members.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("a/conversations.json", "[]")
        archive.writestr("b/conversations.json", "[]")

    with pytest.raises(ChatGPTArchiveError, match="duplicate"):
        parse_chatgpt_export(archive_path)


def test_message_limit_matches_host_and_is_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert parser_module._MAX_MESSAGES_TOTAL == 100_000
    payload = _fixture_payload()
    second = _fixture_payload()[0]
    second["id"] = "conversation-stable-002"
    payload.append(second)
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(parser_module, "_MAX_MESSAGES_TOTAL", 5)

    with pytest.raises(ChatGPTArchiveError, match="too many messages"):
        parse_chatgpt_export(path)


def test_result_warning_collection_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps([None] * 250), encoding="utf-8")

    result = parse_chatgpt_export(path)

    assert len(result.warnings) == 200
    assert result.warnings[-1].code == "warnings_truncated"
    assert sum(item.code == "warnings_truncated" for item in result.warnings) == 1


def test_source_warning_collection_is_bounded(tmp_path: Path) -> None:
    mapping: dict[str, object] = {}
    parent: str | None = None
    for index in range(110):
        node_id = f"null-node-{index}"
        mapping[node_id] = {
            "id": node_id,
            "parent": parent,
            "children": [],
            "message": None,
        }
        parent = node_id
    mapping["valid-node"] = {
        "id": "valid-node",
        "parent": parent,
        "children": [],
        "message": {
            "id": "valid-message",
            "author": {"role": "user"},
            "create_time": 1710001000,
            "content": {"content_type": "text", "parts": ["[TEXT]"]},
        },
    }
    path = tmp_path / "conversations.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "warning-heavy-session",
                    "title": "Warning test",
                    "current_node": "valid-node",
                    "mapping": mapping,
                }
            ]
        ),
        encoding="utf-8",
    )

    result = parse_chatgpt_export(path)

    assert len(result.sessions[0].warnings) == 100
    assert result.sessions[0].warnings[-1].code == "warnings_truncated"
    assert result.warnings == []
