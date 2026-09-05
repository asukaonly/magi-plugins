from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_plugin_module() -> ModuleType:
    plugin_dir = Path(__file__).resolve().parents[1]
    plugins_dir = plugin_dir.parent
    if str(plugins_dir) not in sys.path:
        sys.path.insert(0, str(plugins_dir))
    package_name = "codex_plugin_under_test"
    package = ModuleType(package_name)
    package.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.plugin",
        plugin_dir / "plugin.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_codex_history_registers_only_codex() -> None:
    plugin = _load_plugin_module().CodexPlugin()
    sources = plugin.get_sources()

    assert len(sources) == 1
    _source_id, _source, spec = sources[0]
    assert spec.metadata["source_type"] == "codex_agent_history"
    assert spec.metadata["entry_id"] == "codex"
    assert spec.metadata["capability_id"] == "agent_history"
    assert spec.metadata["default_settings"]["source_paths"] == ["~/.codex"]


def test_codex_history_registers_only_codex_profile() -> None:
    plugin = _load_plugin_module().CodexPlugin()
    profiles = plugin.get_extraction_profiles()

    assert [profile.source_types for profile in profiles] == [["codex_agent_history"]]
    assert profiles[0].allow_assertion is True


def test_codex_history_manifest_is_independent() -> None:
    import tomllib

    plugin_dir = Path(__file__).resolve().parents[1]
    plugin = tomllib.loads((plugin_dir / "plugin.toml").read_text())["plugin"]
    defaults = plugin["default_settings"]["sources"]

    assert plugin["entry_class"] == "CodexPlugin"
    assert plugin["depends_on"] == ["agent_history_core"]
    assert set(defaults) == {"codex_agent_history"}
    assert plugin["display_group"]["member_label"] == "Codex"


def test_codex_history_locales_do_not_claim_claude_access() -> None:
    for locale in ("en.json", "zh-CN.json"):
        text = (
            Path(__file__).resolve().parents[1] / "i18n" / locale
        ).read_text()
        assert "Claude Code" not in text
