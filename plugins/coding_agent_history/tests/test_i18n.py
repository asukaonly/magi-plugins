"""Guard: i18n locale files are valid and cover every facet key the sensor emits."""
import json
from pathlib import Path

_I18N = Path(__file__).resolve().parent.parent / "i18n"
_LOCALES = ("en", "zh-CN")

# Keys the sensor passes as i18n_key (see sensor.py build_output activity facets).
_FACET_KEYS = [
    ("activity", "source", "claude_code"),
    ("activity", "source", "codex"),
    ("activity", "action", "conversed"),
    ("activity", "object", "coding_session"),
]


def _load(locale: str) -> dict:
    return json.loads((_I18N / f"{locale}.json").read_text(encoding="utf-8"))


def test_locale_files_valid_with_plugin_block():
    for locale in _LOCALES:
        data = _load(locale)
        assert "coding_agent_history" in data, locale
        block = data["coding_agent_history"]
        assert block.get("name") and block.get("description"), locale
        assert "source_paths" in block.get("fields", {}), locale


def test_all_sensor_facet_keys_resolve_in_both_locales():
    for locale in _LOCALES:
        data = _load(locale)
        for path in _FACET_KEYS:
            node = data
            for seg in path:
                assert isinstance(node, dict) and seg in node, f"{locale} missing {'.'.join(path)}"
                node = node[seg]
            assert isinstance(node, str) and node.strip(), f"{locale} empty {'.'.join(path)}"
