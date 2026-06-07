from __future__ import annotations
import json
from pathlib import Path


def _leaf_keys(obj, prefix=""):
    out = set()
    for k, v in obj.items():
        p = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out |= _leaf_keys(v, p)
        else:
            out.add(p)
    return out


def test_en_and_zh_have_matching_keys_and_required_namespaces() -> None:
    root = Path(__file__).resolve().parents[1] / "i18n"
    en = json.loads((root / "en.json").read_text(encoding="utf-8"))
    zh = json.loads((root / "zh-CN.json").read_text(encoding="utf-8"))
    assert _leaf_keys(en) == _leaf_keys(zh)
    # Plugin-scoped schema (per the frontend contract): fields live under the plugin id.
    assert "obsidian-vault" in en
    assert "fields" in en["obsidian-vault"]
    # Activity facet i18n keys used by the sensor must resolve.
    assert en["activity"]["source"]["obsidian"]
    assert en["activity"]["object"]["note"]
