from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "chatgpt_history_plugin_test"


def _load_plugin_module():
    package = type(sys)(PACKAGE_NAME)
    package.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = package
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE_NAME}.plugin",
        PLUGIN_DIR / "plugin.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_registers_one_history_importer() -> None:
    plugin = _load_plugin_module().ChatGPTHistoryPlugin()

    registrations = plugin.get_history_importers()

    assert len(registrations) == 1
    importer_id, _importer, spec = registrations[0]
    assert importer_id == "chatgpt_export"
    assert spec.importer_id == importer_id
    assert spec.accepted_extensions == ["zip", "json"]
    assert spec.format_version == "chatgpt-export-v1"
    assert spec.participant_identity_scope == "export"
    assert spec.export_help_url.startswith("https://help.openai.com/")
    assert spec.display_name == "ChatGPT history"
    assert spec.display_name_i18n == {
        "en": "ChatGPT history",
        "zh-CN": "ChatGPT 历史",
    }


def test_importer_keeps_each_conversation_as_a_source() -> None:
    module = _load_plugin_module()
    importer = module.ChatGPTHistoryImporter()
    fixture = PLUGIN_DIR / "tests" / "fixtures" / "conversations.json"

    result = asyncio.run(importer.parse([fixture]))

    assert len(result.sources) == 1
    source = result.sources[0]
    assert source.source_id == "conversation-stable-001"
    assert source.session_key == "conversation-stable-001"
    assert source.detected_kind == "chat"
    assert [record.message_key for record in source.records] == [
        "message-user-1",
        "message-assistant-active",
        "message-user-2",
    ]
    assert [record.role_hint for record in source.records] == [
        "user",
        "assistant",
        "user",
    ]
    assert source.records[1].parent_message_key == "message-user-1"
    assert any("null_message" in warning for warning in source.warnings)


def test_importer_drops_parent_reference_when_parent_content_is_unsupported(
    tmp_path: Path,
) -> None:
    module = _load_plugin_module()
    fixture = PLUGIN_DIR / "tests" / "fixtures" / "conversations.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload[0]["mapping"]["node-assistant-active"]["message"]["content"] = {
        "content_type": "audio",
        "transcript": "do not infer",
    }
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = asyncio.run(module.ChatGPTHistoryImporter().parse([path]))

    source = result.sources[0]
    assert [record.message_key for record in source.records] == [
        "message-user-1",
        "message-user-2",
    ]
    assert source.records[1].parent_message_key is None


def test_non_user_export_roles_are_not_promoted_to_user(tmp_path: Path) -> None:
    module = _load_plugin_module()
    fixture = PLUGIN_DIR / "tests" / "fixtures" / "conversations.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload[0]["mapping"]["node-assistant-active"]["message"]["author"] = {
        "role": "tool",
        "name": "example-tool",
    }
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = asyncio.run(module.ChatGPTHistoryImporter().parse([path]))

    tool_record = result.sources[0].records[1]
    assert tool_record.speaker_id == "tool"
    assert tool_record.speaker_name == "example-tool"
    assert tool_record.role_hint == "other"


def test_importer_bounds_result_source_and_warning_text(tmp_path: Path) -> None:
    module = _load_plugin_module()
    conversations = [None] * 250 + [
        _warning_heavy_conversation(f"session-{index}") for index in range(3)
    ]
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(conversations), encoding="utf-8")

    result = asyncio.run(module.ChatGPTHistoryImporter().parse([path]))

    assert len(result.warnings) == 200
    assert result.warnings[-1] == "warnings_truncated"
    assert all(len(warning) <= 512 for warning in result.warnings)
    assert len(result.sources) == 3
    for source in result.sources:
        assert len(source.warnings) == 100
        assert source.warnings[-1] == "warnings_truncated"
        assert all(len(warning) <= 512 for warning in source.warnings)


def test_importer_applies_total_message_limit_across_selected_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_plugin_module()
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_path.write_text("[]", encoding="utf-8")
    second_path.write_text("[]", encoding="utf-8")
    sessions = [
        _single_message_session(module, session_id="session-1", content="one"),
        _single_message_session(module, session_id="session-2", content="two"),
    ]
    calls = 0

    def parse_selected(path: Path):
        nonlocal calls
        session = sessions[calls]
        calls += 1
        result_type = sys.modules[f"{PACKAGE_NAME}.parser"].ChatGPTArchiveResult
        return result_type(source_name=path.name, sessions=[session])

    monkeypatch.setattr(module, "parse_chatgpt_export", parse_selected)
    monkeypatch.setattr(module, "_MAX_TOTAL_RECORDS", 1)

    with pytest.raises(module.ChatGPTArchiveError, match="too many messages"):
        asyncio.run(module.ChatGPTHistoryImporter().parse([first_path, second_path]))


def test_importer_applies_total_content_limit_before_sdk_modeling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_plugin_module()
    selected = tmp_path / "export.json"
    selected.write_text("[]", encoding="utf-8")
    session = _single_message_session(
        module,
        session_id="large-session",
        content="large",
    )
    monkeypatch.setattr(
        module,
        "parse_chatgpt_export",
        lambda path: sys.modules[f"{PACKAGE_NAME}.parser"].ChatGPTArchiveResult(
            source_name=path.name,
            sessions=[session],
        ),
    )
    monkeypatch.setattr(module, "_MAX_TOTAL_CONTENT_CHARS", 4)

    with pytest.raises(module.ChatGPTArchiveError, match="too much message text"):
        asyncio.run(module.ChatGPTHistoryImporter().parse([selected]))


def test_importer_deduplicates_identical_session_across_selected_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_plugin_module()
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_path.write_text("[]", encoding="utf-8")
    second_path.write_text("[]", encoding="utf-8")
    session = _single_message_session(
        module,
        session_id="shared-session",
        content="same message",
    )
    result_type = sys.modules[f"{PACKAGE_NAME}.parser"].ChatGPTArchiveResult
    monkeypatch.setattr(
        module,
        "parse_chatgpt_export",
        lambda path: result_type(source_name=path.name, sessions=[session]),
    )

    result = asyncio.run(
        module.ChatGPTHistoryImporter().parse([first_path, second_path])
    )

    assert [source.source_id for source in result.sources] == ["shared-session"]
    assert any("duplicate_session" in warning for warning in result.warnings)


def test_importer_rejects_conflicting_session_across_selected_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_plugin_module()
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_path.write_text("[]", encoding="utf-8")
    second_path.write_text("[]", encoding="utf-8")
    sessions = [
        _single_message_session(
            module,
            session_id="shared-session",
            content="old message",
        ),
        _single_message_session(
            module,
            session_id="shared-session",
            content="new message",
        ),
    ]
    calls = 0

    def parse_selected(path: Path):
        nonlocal calls
        result_type = sys.modules[f"{PACKAGE_NAME}.parser"].ChatGPTArchiveResult
        result = result_type(source_name=path.name, sessions=[sessions[calls]])
        calls += 1
        return result

    monkeypatch.setattr(module, "parse_chatgpt_export", parse_selected)

    with pytest.raises(module.ChatGPTArchiveError, match="conflicting versions"):
        asyncio.run(module.ChatGPTHistoryImporter().parse([first_path, second_path]))


def _single_message_session(module, *, session_id: str, content: str):
    message_type = sys.modules[f"{PACKAGE_NAME}.parser"].ChatGPTMessage
    return module.ChatGPTSession(
        session_id=session_id,
        source_key=session_id,
        title=session_id,
        created_at=None,
        updated_at=None,
        messages=(
            message_type(
                message_id=f"message-{session_id}",
                message_key=f"message-{session_id}",
                parent_message_id=None,
                source_order=0,
                speaker_id="user",
                speaker_name="User",
                role_hint="user",
                occurred_at=None,
                content=content,
                content_type="text",
            ),
        ),
        source_name="conversations.json",
    )


def _warning_heavy_conversation(session_id: str) -> dict[str, object]:
    mapping: dict[str, object] = {}
    parent: str | None = None
    for index in range(110):
        node_id = f"null-node-{index}-" + "m" * 600
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
            "id": f"valid-message-{session_id}",
            "author": {"role": "user"},
            "create_time": 1710001000,
            "content": {"content_type": "text", "parts": ["[TEXT]"]},
        },
    }
    return {
        "id": session_id,
        "title": "Warning test",
        "current_node": "valid-node",
        "mapping": mapping,
    }
