from __future__ import annotations

from sdk_test_support import bind_test_plugin

import importlib.util
import sys
from pathlib import Path


def _load_plugin_class():
    plugin_dir = Path(__file__).resolve().parents[1]
    pkg = "local_documents_plugin_under_test"
    spec = importlib.util.spec_from_file_location(
        pkg, plugin_dir / "__init__.py", submodule_search_locations=[str(plugin_dir)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[pkg] = package
    spec.loader.exec_module(package)
    pspec = importlib.util.spec_from_file_location(f"{pkg}.plugin", plugin_dir / "plugin.py")
    module = importlib.util.module_from_spec(pspec)
    sys.modules[pspec.name] = module
    pspec.loader.exec_module(module)
    return module.LocalDocumentsPlugin


def _make_plugin(enabled: bool):
    cls = _load_plugin_class()
    plugin = bind_test_plugin(cls())
    plugin.settings = {
        "sources": {
            "local_documents": {
                "enabled": enabled,
                "root_paths": ["/tmp/notes", "/tmp/docs"],
                "exclude_folders": [".git"],
                "cognition_exclude_folders": ["References"],
                "include_extensions": [".md", ".txt"],
            }
        }
    }
    return plugin


def test_get_sources_returns_knowledge_and_search_tiers_when_enabled() -> None:
    plugin = _make_plugin(enabled=True)
    sources = plugin.get_sources()

    ids = {source_id for source_id, _inst, _spec in sources}
    assert ids == {"timeline.local_documents.knowledge", "timeline.local_documents.search"}
    source_types = {source_id: inst.source_type for source_id, inst, _ in sources}
    assert source_types["timeline.local_documents.knowledge"] == "local_documents"
    assert source_types["timeline.local_documents.search"] == "local_documents_search"
    policies = {source_id: inst.memory_policy.cognition_eligible for source_id, inst, _ in sources}
    assert policies["timeline.local_documents.knowledge"] is True
    assert policies["timeline.local_documents.search"] is False
    spec_source_types = {source_id: spec.metadata["source_type"] for source_id, _inst, spec in sources}
    assert spec_source_types["timeline.local_documents.search"] == "local_documents_search"
    for _, _, spec in sources:
        assert spec.metadata["capability_id"] == "local_documents"
        assert spec.metadata["entry_id"] == "local_documents"
        assert spec.metadata["entry_display_name"] == "Local Documents"


def test_get_sources_stay_discoverable_when_disabled() -> None:
    sources = _make_plugin(enabled=False).get_sources()

    assert len(sources) == 2
    for _source_id, _source, spec in sources:
        flow = spec.metadata["activation_flow"]
        assert flow["enabled_key"] == "sources.local_documents.enabled"
        assert flow["first_context"]["max_items_per_sync"] == 200


def test_extraction_profile_only_targets_knowledge_source() -> None:
    plugin = _make_plugin(enabled=True)
    profile = plugin.get_extraction_profiles()[0]

    assert profile.profile_id == "source.local_documents"
    assert profile.source_types == ["local_documents"]
    assert "REFERENCES" in profile.allowed_predicates
    assert list(profile.structured_allowed_predicates) == ["REFERENCES"]
    assert "note" not in profile.allowed_entity_types


def test_summary_profile_covers_both_tiers() -> None:
    plugin = _make_plugin(enabled=True)
    summary = plugin.get_summary_profiles()[0]

    assert summary.profile_id == "local-documents:document_activity"
    assert summary.summary_category == "document_activity"
    assert set(summary.source_types) == {"local_documents", "local_documents_search"}
