"""Settings exports are static, lossless and independent of execution state."""
from __future__ import annotations

import builtins
import importlib.util
import io
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tomllib

import pytest
from magi_plugin_sdk import Plugin, PluginManifest, Source
from pydantic import ValidationError

from settings_declarations import CATALOG_KEYS, read_manifest, settings_catalog

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = sorted((ROOT / "plugins").glob("*/plugin.toml"))
_spec = importlib.util.spec_from_file_location("export_settings_fields", ROOT / "scripts/export-settings-fields.py")
exporter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(exporter)


@pytest.mark.parametrize("path", PACKAGES, ids=lambda path: path.parent.name)
@pytest.mark.parametrize("runtime_platform", ["linux", "darwin", "win32"])
def test_export_is_lossless_idempotent_and_has_no_runtime_effects(path, runtime_platform, monkeypatch):
    original = path.read_bytes()
    expected = settings_catalog(PluginManifest.model_validate(tomllib.loads(original.decode())["plugin"]))
    original_open = io.open

    def reject(*args, **kwargs):
        raise AssertionError("Static declarations must not import or execute runtime code")

    def manifest_only(file, mode="r", *args, **kwargs):
        assert Path(file) == path
        assert mode in {"r", "rb"}, "Export must not write the manifest"
        return original_open(file, mode, *args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(sys, "platform", runtime_platform)
        guard.setattr(Plugin, "__init__", reject)
        guard.setattr(Plugin, "get_sources", reject)
        guard.setattr(Source, "__init__", reject)
        guard.setattr(Path, "home", reject)
        guard.setattr(Path, "exists", reject)
        guard.setattr(importlib.util, "find_spec", reject)
        guard.setattr(subprocess, "Popen", reject)
        guard.setattr(socket, "socket", reject)
        guard.setattr(sqlite3, "connect", reject)
        guard.setattr(builtins, "open", reject)
        guard.setattr(io, "open", manifest_only)
        guard.setattr(builtins, "__import__", reject)
        first = exporter.export(path)
        second = exporter.export(path)
    assert first == second == expected
    assert set(first) == set(CATALOG_KEYS)
    assert path.read_bytes() == original
    # Export callers cannot mutate the next export through shared model objects.
    first["settings_fields"].clear()
    assert exporter.export(path) == expected


@pytest.mark.parametrize("directory,prefix", [
    ("local-documents", "sources.local_documents"),
    ("obsidian-vault", "sources.obsidian_vault"),
])
def test_multisource_export_keeps_primary_activation_and_all_fields(directory, prefix):
    path = ROOT / "plugins" / directory / "plugin.toml"
    manifest = read_manifest(path)
    catalog = exporter.export(path)
    assert len(manifest.projection_sources) == 2
    assert catalog["activation_flow"]["enabled_key"] == f"{prefix}.enabled"
    assert catalog["activation_flow"]["configured_key"] == f"{prefix}.initial_sync_configured"
    assert catalog["activation_flow"]["first_context"]["max_items_per_sync"] == 200
    assert {field["key"] for field in catalog["settings_fields"]} == {
        field.key for field in manifest.settings_fields
    }
    assert {field["key"] for field in catalog["activation_flow"]["fields"]} <= {
        field["key"] for field in catalog["settings_fields"]
    }


def test_export_retains_setup_catalog_and_hidden_credentials():
    for directory in ("weixin", "github_activity", "calendar_plugin", "screenshot_timeline", "apple-photos"):
        path = ROOT / "plugins" / directory / "plugin.toml"
        manifest = read_manifest(path)
        catalog = exporter.export(path)
        assert catalog == settings_catalog(manifest)
        assert catalog["settings_actions"] or catalog["settings_resources"]
    github = exporter.export(ROOT / "plugins/github_activity/plugin.toml")
    fields = {field["key"]: field for field in github["settings_fields"]}
    assert fields["sources.github_activity.access_token"]["type"] == "secret"
    assert fields["sources.github_activity.client_id"]["section"] == "advanced"
    assert any(not action["requires_enabled"] for action in github["settings_actions"])


def test_invalid_manifest_is_rejected_without_repair_or_partial_export(tmp_path):
    path = tmp_path / "plugin.toml"
    text = (ROOT / "plugins/calendar_plugin/plugin.toml").read_text()
    text = text.replace('maximum = 365.0', 'maximum = -1.0', 1)
    assert 'maximum = -1.0' in text
    path.write_text(text)
    with pytest.raises(ValidationError):
        exporter.export(path)
    assert path.read_text() == text


def test_cli_exports_validated_json_and_check_is_read_only():
    path = ROOT / "plugins/calendar_plugin/plugin.toml"
    original = path.read_bytes()
    command = [sys.executable, str(ROOT / "scripts/export-settings-fields.py"), str(path)]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == {str(path): exporter.export(path)}
    result = subprocess.run([*command, "--check"], check=True, capture_output=True, text=True)
    assert "Validated settings declarations for 1 packages" in result.stdout
    assert path.read_bytes() == original
