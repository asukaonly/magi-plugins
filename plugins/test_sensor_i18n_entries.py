"""Guard sensor list display names used by the Magi settings UI."""
from __future__ import annotations

import json
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent

SENSOR_ENTRY_PLUGINS = {
    "screen_time": ["screen_time"],
    "coding_agent_history": ["claude_code", "codex"],
    "git_activity": ["git_activity"],
    "github_activity": ["github_activity"],
    "netease_music": ["netease_music"],
    "screenshot_timeline": ["screenshot_timeline"],
    "steam_play_history": ["steam"],
    "terminal_history": ["terminal_history"],
}


def _load_i18n(plugin_dir: str, locale: str) -> dict:
    return json.loads((PLUGIN_ROOT / plugin_dir / "i18n" / f"{locale}.json").read_text())


def test_sensor_status_entries_have_localized_display_text() -> None:
    for plugin_dir, entry_ids in SENSOR_ENTRY_PLUGINS.items():
        for locale in ("en", "zh-CN"):
            payload = _load_i18n(plugin_dir, locale)
            root = payload[plugin_dir]
            for entry_id in entry_ids:
                entry = root["entries"][entry_id]
                assert entry["display_name"].strip(), f"{plugin_dir} {locale}"
                assert entry["description"].strip(), f"{plugin_dir} {locale}"


def test_app_usage_uses_chinese_entry_label() -> None:
    payload = _load_i18n("screen_time", "zh-CN")
    assert payload["screen_time"]["entries"]["screen_time"]["display_name"] == "应用使用"
