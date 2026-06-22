"""Tests for generated marketplace registry metadata."""
from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-registry.py"


def _load_build_registry_module():
    spec = importlib.util.spec_from_file_location("build_registry", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_browser_history_plugins_declare_marketplace_display_group() -> None:
    build_registry = _load_build_registry_module()

    expected = {
        "chrome-history": ("Chrome", 10),
        "safari-history": ("Safari", 20),
        "firefox-history": ("Firefox", 30),
        "edge-history": ("Edge", 40),
    }

    for plugin_dir, (member_label, member_order) in expected.items():
        entry = build_registry.build_entry(ROOT / "plugins" / plugin_dir, official_ids=set())
        assert entry is not None
        group = entry["display_group"]
        assert group["id"] == "browser_history"
        assert group["name"] == "Browser History"
        assert group["name_i18n"]["zh-CN"] == "浏览历史"
        assert group["icon"] == "lucide:globe"
        assert group["member_label"] == member_label
        assert group["member_order"] == member_order
