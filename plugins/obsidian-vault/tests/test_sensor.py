# tests/test_sensor.py
from __future__ import annotations
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_sensor_module() -> ModuleType:
    plugin_dir = Path(__file__).resolve().parents[1]
    pkg_name = "obsidian_vault_under_test"
    spec = importlib.util.spec_from_file_location(
        pkg_name, plugin_dir / "__init__.py", submodule_search_locations=[str(plugin_dir)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = package
    spec.loader.exec_module(package)
    sensor_spec = importlib.util.spec_from_file_location(f"{pkg_name}.sensor", plugin_dir / "sensor.py")
    module = importlib.util.module_from_spec(sensor_spec)
    sys.modules[sensor_spec.name] = module
    sensor_spec.loader.exec_module(module)
    return module


def _sample_item() -> dict:
    return {
        "rel_path": "Projects/Magi.md",
        "uid": "",
        "title": "Magi Project",
        "body": "Working with [[Alex]] on the launch. #beta",
        "tags": ["project", "beta"],
        "wikilinks": ["Alex", "Project X"],
        "aliases": ["Magi"],
        "frontmatter": {"title": "Magi Project"},
        "mtime": 1781000000.0,
    }


def test_build_output_maps_note_to_l1_fields() -> None:
    mod = _load_sensor_module()
    sensor = mod.ObsidianVaultSensor(cognition_eligible=True, sensor_suffix="knowledge")
    out = asyncio.run(sensor.build_output(_sample_item()))

    assert out.source_type == "obsidian_vault"
    # Stable id = vault-relative path when no frontmatter uid.
    assert out.source_item_id == "Projects/Magi.md"
    assert out.occurred_at == 1781000000.0
    assert out.narration.title == "Magi Project"
    assert "Working with" in out.narration.body  # full text, not a summary
    assert out.activity.source.code == "obsidian"
    assert out.activity.object is not None and out.activity.object.code == "note"
    assert set(out.tags) == {"project", "beta"}
    assert out.activity.qualifiers["wikilink_count"] == 2


def test_build_output_prefers_frontmatter_uid_for_supersession() -> None:
    mod = _load_sensor_module()
    sensor = mod.ObsidianVaultSensor(cognition_eligible=True, sensor_suffix="knowledge")
    item = _sample_item()
    item["uid"] = "note-uid-123"
    out = asyncio.run(sensor.build_output(item))
    assert out.source_item_id == "note-uid-123"


def test_memory_policy_differs_by_tier() -> None:
    mod = _load_sensor_module()
    knowledge = mod.ObsidianVaultSensor(cognition_eligible=True, sensor_suffix="knowledge")
    search = mod.ObsidianVaultSensor(cognition_eligible=False, sensor_suffix="search")
    assert knowledge.memory_policy.cognition_eligible is True
    assert search.memory_policy.cognition_eligible is False
    # Both are authored + permanent.
    assert knowledge.memory_policy.memory_domain == "user_authored"
    assert knowledge.memory_policy.retention_class == "permanent"


def test_extract_metadata_emits_entities_and_relations() -> None:
    mod = _load_sensor_module()
    sensor = mod.ObsidianVaultSensor(cognition_eligible=True, sensor_suffix="knowledge")
    meta = asyncio.run(sensor.extract_metadata(_sample_item()))

    # The note itself + each wikilink target become entity hints.
    surfaces = {e["surface"] for e in meta.entities}
    assert "Magi Project" in surfaces      # the note
    assert "Alex" in surfaces and "Project X" in surfaces
    assert set(meta.tags) == {"project", "beta"}

    # Each wikilink is a REFERENCES relation candidate from this note.
    preds = {(rc["predicate"], rc["object_ref"]) for rc in meta.relation_candidates}
    assert ("REFERENCES", "Alex") in preds
    assert ("REFERENCES", "Project X") in preds
