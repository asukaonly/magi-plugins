"""Contract conformance using only public SDK classes and declared libraries."""
from __future__ import annotations

import ast
import importlib.util
import inspect
import math
import sys
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest
from magi_plugin_sdk import ExtensionFieldSpec, PluginManifest, Source
from magi_plugin_sdk.runtime import SDK_VERSION, OperationSpec
from magi_plugin_sdk.tools import ToolResult
from magi_plugin_sdk.versioning import parse_plugin_version
from sdk_test_support import bind_test_plugin, load_plugin

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = sorted((ROOT / "plugins").glob("*/plugin.toml"))
RUNTIME_PLATFORMS = {"macos": "darwin", "windows": "win32", "linux": "linux"}
DECLARATION_CASES = [
    pytest.param(path, runtime_platform, id=f"{path.parent.name}-{platform}")
    for path in PACKAGES
    for platform, runtime_platform in RUNTIME_PLATFORMS.items()
    if platform in tomllib.loads(path.read_text())["plugin"].get("platforms", RUNTIME_PLATFORMS)
]


@pytest.mark.parametrize("path", PACKAGES, ids=lambda path: path.parent.name)
def test_package_uses_explicit_protocol_and_public_sdk(path: Path) -> None:
    meta = tomllib.loads(path.read_text())["plugin"]
    assert meta["protocol_version"] == 2
    assert parse_plugin_version(meta["min_sdk_version"]) <= parse_plugin_version(SDK_VERSION)
    assert meta["execution_mode"] == "trusted_process"
    assert isinstance(meta["projection_sources"], list)
    assert isinstance(meta["settings_fields"], list)
    keys = [field["key"] for field in meta["settings_fields"]]
    assert len(keys) == len(set(keys)), "Settings keys must be unique"
    PluginManifest.model_validate(meta)
    for source in path.parent.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").split(".")[0] == "magi", source
            elif isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] != "magi" for alias in node.names), source


@pytest.mark.parametrize("path,runtime_platform", DECLARATION_CASES)
def test_all_declarations_construct_without_backend(path: Path, runtime_platform: str) -> None:
    before = {name for name in sys.modules if name == "magi" or name.startswith("magi.")}
    if tomllib.loads(path.read_text())["plugin"].get("kind") == "library":
        for source_file in path.parent.glob("*.py"):
            if source_file.name == "__init__.py":
                continue
            __import__(f"{path.parent.name}.{source_file.stem}")
        return
    plugin = bind_test_plugin(load_plugin(path))
    for method_name in ("get_extraction_profiles", "get_summary_profiles", "get_settings_resources", "get_settings_actions", "get_channel_fields", "get_operations"):
        method = getattr(plugin, method_name, None)
        if method is not None:
            method()
    declared_fields = {field.key: field for field in plugin.manifest.settings_fields}
    for field in plugin.get_channel_fields():
        assert field.key in declared_fields
        assert field.type == declared_fields[field.key].type
    with patch.object(sys, "platform", runtime_platform):
        declarations = plugin.get_sources()
    assert len({source_id for source_id, _, _ in declarations}) == len(declarations)
    for source_id, source, spec in declarations:
        assert isinstance(source, Source)
        assert source_id == source.source_id == spec.source_id
        assert isinstance(source.source_type, str)
        assert source.source_type in plugin.manifest.projection_sources
        for field in spec.fields:
            assert isinstance(field, ExtensionFieldSpec)
            assert field.key in declared_fields
            assert field.type == declared_fields[field.key].type
            assert field.minimum == declared_fields[field.key].minimum
            assert field.maximum == declared_fields[field.key].maximum
            assert field.default == declared_fields[field.key].default
        original = {"source_item_id": "same-native-id", "body": "original"}
        revised = {**original, "body": "edited", "description": "new metadata"}
        assert source.source_item_version_fingerprint(original) != source.source_item_version_fingerprint(revised)
        if source.supports_pull_sync:
            assert inspect.signature(source.collect_items).return_annotation in {"SourceChangeBatch"}
    for tool_class in plugin.get_tools():
        schema = tool_class().schema
        OperationSpec(
            operation_id=schema.name, description=schema.description,
            input_schema=schema.json_input_schema(), output_schema=schema.output_schema,
            triggers=["user", "model"], effect=schema.effect_class,
            replay=schema.effect_replay_policy,
            idempotency_key_parameter=schema.effect_idempotency_key_parameter,
        )
    for _, importer, _ in plugin.get_history_importers():
        assert callable(importer.parse)
    after = {name for name in sys.modules if name == "magi" or name.startswith("magi.")}
    assert after == before


def test_connections_isolate_channel_credentials_and_storage() -> None:
    for package_name in ("telegram", "weixin"):
        path = ROOT / "plugins" / package_name / "plugin.toml"
        first = bind_test_plugin(load_plugin(path), connection_id="account-one", settings={"bot_token": "ignored-settings-token"})
        second = bind_test_plugin(load_plugin(path), connection_id="account-two")
        first.context.credentials.set("bot_token", "first-token")
        second.context.credentials.set("bot_token", "second-token")
        assert first.get_channel()._config.bot_token == "first-token"
        assert second.get_channel()._config.bot_token == "second-token"
        first.context.credentials.delete("bot_token")
        assert second.context.credentials.get("bot_token") == "second-token"
        assert first.context.state_dir != second.context.state_dir


def test_weixin_content_clear_cannot_erase_other_account_or_credentials() -> None:
    path = ROOT / "plugins" / "weixin" / "plugin.toml"
    first = bind_test_plugin(load_plugin(path), connection_id="first")
    second = bind_test_plugin(load_plugin(path), connection_id="second")
    state_module = sys.modules[first.__class__.__module__.rsplit(".", 1)[0] + ".state"]
    stores = [first._state_store(), second._state_store()]
    for index, store in enumerate(stores):
        store.save_credentials(state_module.WeixinCredentials(account_id="same-provider-id", token=f"private-token-{index}"))
        store.save_context_tokens("same-provider-id", {"chat": "private conversation"})
    stores[0].clear_inbound_content(clear_generation=1)
    assert stores[0].load_credentials().token == "private-token-0"
    assert stores[1].load_credentials().token == "private-token-1"
    assert stores[1].load_context_tokens("same-provider-id") == {"chat": "private conversation"}
    assert stores[0].load_context_tokens("same-provider-id") == {}
    for store in stores:
        assert all("private-token" not in file.read_text() for file in store.state_dir.rglob("*") if file.is_file())


def test_screenshot_collector_and_resolver_use_connection_resources() -> None:
    path = ROOT / "plugins" / "screenshot_timeline" / "plugin.toml"
    first = bind_test_plugin(load_plugin(path), connection_id="first")
    second = bind_test_plugin(load_plugin(path), connection_id="second")
    first_source = first.get_sources()[0][1]
    second_source = second.get_sources()[0][1]
    assert first_source.resources_root == first.context.resources_dir
    assert first_source._session_db_path.parent == first.context.resources_dir
    assert first_source.resources_root != second_source.resources_root


def test_local_documents_preserve_category_but_isolate_data_and_versions(tmp_path: Path) -> None:
    import asyncio
    from magi_plugin_sdk.runtime import SourceChangeBatch
    from magi_plugin_sdk.sources import SourceSyncContext

    path = ROOT / "plugins" / "local-documents" / "plugin.toml"
    results = []
    sources = []
    for connection_id in ("first", "second"):
        root = tmp_path / connection_id
        root.mkdir()
        (root / "notes.md").write_text(f"# {connection_id}\nPrivate notes")
        plugin = bind_test_plugin(load_plugin(path), connection_id=connection_id, settings={
            "sources": {"local_documents": {"root_paths": [str(root)]}},
        })
        source = plugin.get_sources()[0][1]
        context = SourceSyncContext(
            connection_id=plugin.connection.connection_id, source_type=source.source_type,
            manual=True, last_cursor=None, last_success_at=None, limit=50,
            runtime_paths=None, plugin_settings=plugin.settings,
        )
        batch = asyncio.run(source.collect_items(context))
        batch = SourceChangeBatch.model_validate_json(batch.model_dump_json())
        assert len(batch.changes) == 1
        assert connection_id in batch.changes[0].payload["body"]
        results.append(batch)
        sources.append(source)
    assert sources[0].source_type == sources[1].source_type == "local_documents"
    first = results[0].changes[0]
    edited = {**first.payload, "body": "Edited notes"}
    assert sources[0].source_item_identity(edited) == first.object_id
    assert sources[0].source_item_version_fingerprint(edited) != first.version


@pytest.mark.parametrize("path", PACKAGES, ids=lambda path: path.parent.name)
def test_declared_schema_covers_defaults_and_credential_ports(path: Path) -> None:
    meta = tomllib.loads(path.read_text())["plugin"]
    fields = {field["key"]: field for field in meta["settings_fields"]}

    def flatten(value, prefix=""):
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else key
            if isinstance(item, dict):
                yield from flatten(item, name)
            else:
                yield name

    assert set(flatten(meta.get("default_settings", {}))) <= fields.keys()
    for source in path.parent.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8-sig"))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"get", "set", "delete"}:
                continue
            receiver = ast.unparse(node.func.value)
            if not receiver.endswith(".credentials") or not node.args:
                continue
            key = node.args[0]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                assert fields[key.value]["type"] == "secret", (source, key.value)


def _assert_default_matches_field(field: ExtensionFieldSpec, value: object) -> None:
    assert field.type != "secret", field.key
    if field.type == "switch":
        assert isinstance(value, bool), field.key
    elif field.type == "number":
        assert not isinstance(value, bool) and isinstance(value, (int, float)), field.key
        assert math.isfinite(value), field.key
        assert field.minimum is None or value >= field.minimum, field.key
        assert field.maximum is None or value <= field.maximum, field.key
    elif field.type == "tags" or (field.type == "path" and isinstance(field.default, list)):
        assert isinstance(value, list) and all(isinstance(item, str) for item in value), field.key
    else:
        assert isinstance(value, str), field.key
        if field.type == "select":
            assert value in {option.value for option in field.options}, field.key


@pytest.mark.parametrize("path", PACKAGES, ids=lambda path: path.parent.name)
def test_settings_defaults_match_host_declarations(path: Path) -> None:
    manifest = PluginManifest.model_validate(tomllib.loads(path.read_text())["plugin"])
    fields = {field.key: field for field in manifest.settings_fields}

    def check_values(values: dict, prefix: str = "") -> None:
        for key, value in values.items():
            name = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                check_values(value, name)
            else:
                _assert_default_matches_field(fields[name], value)

    check_values(manifest.default_settings)
    for field in fields.values():
        if field.type == "secret":
            assert field.default in (None, ""), field.key
        elif field.default is not None:
            _assert_default_matches_field(field, field.default)
    if manifest.kind != "library":
        plugin = bind_test_plugin(load_plugin(path))
        for _, _, spec in plugin.get_sources():
            prefix = next((field.key.rsplit(".", 1)[0] for field in spec.fields if field.key.startswith("sources.")), "")
            check_values(spec.metadata.get("default_settings", {}), prefix)


@pytest.mark.parametrize("path,runtime_platform", DECLARATION_CASES)
def test_declarative_activation_matches_primary_source(path: Path, runtime_platform: str) -> None:
    meta = tomllib.loads(path.read_text())["plugin"]
    if meta.get("kind") == "library":
        return
    plugin = bind_test_plugin(load_plugin(path))
    with patch.object(sys, "platform", runtime_platform):
        flows = [spec.metadata["activation_flow"] for _, _, spec in plugin.get_sources() if spec.metadata.get("activation_flow")]
    if flows:
        assert plugin.manifest.activation_flow.model_dump() == flows[0]
    else:
        assert plugin.manifest.activation_flow is None


@pytest.mark.parametrize("package_name", ["local-documents", "obsidian-vault"])
def test_bounded_document_batches_do_not_skip_equal_timestamps(package_name: str, tmp_path: Path) -> None:
    import asyncio
    import os
    from magi_plugin_sdk.sources import SourceSyncContext

    for name in ("a.md", "b.md", "c.md"):
        file = tmp_path / name
        file.write_text(f"# {name}")
        os.utime(file, (1700000000, 1700000000))
    source_type = "local_documents" if package_name == "local-documents" else "obsidian_vault"
    settings = {"root_paths": [str(tmp_path)]} if package_name == "local-documents" else {"vault_path": str(tmp_path)}
    plugin = bind_test_plugin(load_plugin(ROOT / "plugins" / package_name / "plugin.toml"), settings={"sources": {source_type: settings}})
    source = plugin.get_sources()[0][1]
    context = SourceSyncContext(connection_id=plugin.connection.connection_id, source_type=source_type, manual=True, last_cursor=None, last_success_at=None, limit=1, runtime_paths=None, plugin_settings=plugin.settings)
    assert isinstance(context.source_type, str)
    assert context.source_type == source.source_type
    seen = []
    for _ in range(3):
        result = asyncio.run(source.collect_items(context))
        assert len(result.changes) == 1
        seen.append(result.changes[0].object_id)
        context.last_cursor = result.next_cursor
    assert len(set(seen)) == 3
    assert result.complete is True
    assert asyncio.run(source.collect_items(context)).changes == []


def test_declarative_setup_catalog_matches_public_schemas() -> None:
    for path in PACKAGES:
        meta = tomllib.loads(path.read_text())["plugin"]
        for key in ("settings_actions", "settings_resources", "settings_ui_blocks"):
            assert isinstance(meta[key], list)
        if meta.get("kind") == "library":
            continue
        plugin = bind_test_plugin(load_plugin(path))
        assert [entry.model_dump() for entry in plugin.manifest.settings_actions] == [entry.model_dump() for entry in plugin.get_settings_actions()]
        assert [entry.model_dump() for entry in plugin.manifest.settings_resources] == [entry.model_dump() for entry in plugin.get_settings_resources()]
        assert all(not resource.requires_enabled for resource in plugin.manifest.settings_resources)
        blocks = {entry.block_id: entry.model_dump() for entry in plugin.manifest.settings_ui_blocks}
        for _, _, spec in plugin.get_sources():
            for block in spec.metadata.get("settings_ui_blocks", []):
                assert blocks[block["block_id"]] == block
        if meta["id"] in {"weixin", "github-activity"}:
            assert any(not action.requires_enabled for action in plugin.manifest.settings_actions)


@pytest.mark.parametrize("directory,keys", [
    ("github_activity", {"client_id", "access_token"}),
    ("steam_play_history", {"account_id", "excluded_appids", "excluded_keywords"}),
    ("git_activity", {"session_window_minutes", "max_messages_per_session"}),
    ("terminal_history", {"dedup_window_seconds"}),
])
def test_internal_source_controls_are_declared(directory: str, keys: set[str]) -> None:
    meta = tomllib.loads((ROOT / "plugins" / directory / "plugin.toml").read_text())["plugin"]
    declared = {entry["key"] for entry in meta["settings_fields"]}
    assert {f"sources.{directory}.{key}" for key in keys} <= declared


def test_sdk_exposes_separate_model_observation_contract() -> None:
    result = ToolResult(success=True, data={"id": "original"}, model_text="Readable result")
    assert result.model_text == "Readable result"
    assert result.data == {"id": "original"}
