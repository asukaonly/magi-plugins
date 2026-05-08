from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_normalizers() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "normalizers.py"
    spec = importlib.util.spec_from_file_location("chrome_history_normalizers", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_fandom_title_preserves_chinese_subjects() -> None:
    normalizers = _load_normalizers()

    hints = normalizers.parse_title_entities(
        "蠕动的饥饿 | 诡秘之主 Wiki | Fandom",
        "lordofthemysteries.fandom.com",
    )

    hint_pairs = {(hint["mention_text"], hint["entity_type"]) for hint in hints}
    assert ("Fandom", "software") in hint_pairs
    assert ("蠕动的饥饿", "topic") in hint_pairs
    assert ("诡秘之主", "media") in hint_pairs
    assert all("lordofthemysteries" not in hint["canonical_name_hint"] for hint in hints)


def test_parse_domain_known_wiki_title_without_platform_suffix() -> None:
    normalizers = _load_normalizers()

    hints = normalizers.parse_title_entities(
        "蠕动的饥饿 | 诡秘之主 Wiki",
        "lordofthemysteries.fandom.com",
    )

    names = [hint["mention_text"] for hint in hints]
    assert names == ["Fandom", "蠕动的饥饿", "诡秘之主"]