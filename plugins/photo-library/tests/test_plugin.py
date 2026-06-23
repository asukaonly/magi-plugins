from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_plugin_class():
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "photo_library_plugin_under_test"
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
    return module.PhotoLibraryPlugin


def test_photo_library_declares_graph_only_l2_profiles() -> None:
    cls = _load_plugin_class()
    plugin = cls()
    profiles = plugin.get_extraction_profiles()

    by_source = {profile.source_types[0]: profile for profile in profiles}
    assert set(by_source) == {"photo_library_apple_photos", "photo_library_directory"}

    for source_type, profile in by_source.items():
        assert profile.profile_id == f"source.{source_type}"
        assert profile.allow_graph is True
        assert profile.allow_assertion is False
        assert profile.assertion_mode == "none"
        assert profile.allowed_assertion_families == []
        assert profile.allowed_entity_types == ["hardware", "place"]
        assert profile.allowed_predicates == ["OWNS", "VISITED"]
        assert profile.structured_allowed_entity_types == ["hardware", "place"]
        assert profile.structured_allowed_predicates == ["OWNS", "VISITED"]
        assert "preference_profile" not in profile.allowed_assertion_families
