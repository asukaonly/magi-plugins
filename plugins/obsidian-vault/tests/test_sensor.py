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
    # narration.body is the lean summary (RFC #56 P3); for a one-line note it
    # equals the line. The full note body is pinned for L2 separately.
    assert "Working with" in out.narration.body
    assert out.pinned_payload == _sample_item()["body"]
    assert out.activity.source.code == "obsidian"
    assert out.activity.object is not None and out.activity.object.code == "note"
    assert set(out.tags) == {"project", "beta"}
    assert out.activity.qualifiers["wikilink_count"] == 2


def test_build_output_pins_full_body_and_leans_the_narration() -> None:
    # RFC #56 P3: L1 stays lean (narration = first-line summary, no full body in
    # content blocks); the complete frozen note goes to pinned_payload for L2.
    mod = _load_sensor_module()
    sensor = mod.ObsidianVaultSensor(cognition_eligible=True, sensor_suffix="knowledge")
    long_body = "First line is the summary.\n\n" + ("more detail about the project. " * 80)
    item = {**_sample_item(), "body": long_body}
    out = asyncio.run(sensor.build_output(item))

    # full frozen body reaches L2 via the pinned payload...
    assert out.pinned_payload == long_body
    # ...while L1's narration is just the lean first line
    assert out.narration.body == "First line is the summary."
    assert len(out.narration.body) < len(long_body)
    # and the full body is not duplicated into content blocks (which land in L1 metadata)
    text_blocks = [b.value for b in out.content_blocks if b.kind == "text"]
    assert all("more detail" not in v for v in text_blocks)


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


def test_extract_metadata_emits_host_contract_hints() -> None:
    mod = _load_sensor_module()
    sensor = mod.ObsidianVaultSensor(cognition_eligible=True, sensor_suffix="knowledge")
    meta = asyncio.run(sensor.extract_metadata(_sample_item()))

    # Entity hints use the keys the L2 entity-hint reader requires (it skips items
    # without mention_text); no legacy surface/normalized_name keys.
    by_mention = {e["mention_text"]: e for e in meta.entities}
    assert by_mention["Magi Project"]["entity_type"] == "concept"          # the note
    assert by_mention["Magi Project"]["canonical_name_hint"] == "Magi Project"
    assert by_mention["Alex"]["entity_type"] == "concept"                  # wikilink target
    assert by_mention["beta"]["entity_type"] == "topic"                    # tag
    assert all("surface" not in e for e in meta.entities)
    assert set(meta.tags) == {"project", "beta"}

    # Relations go through fact_hints (NOT relation_candidates), in the exact host shape:
    # type:name refs, REFERENCES predicate, explicit_fact + source_structured so the
    # deterministic direct-write path admits them.
    assert meta.relation_candidates == []
    edges = {(f["predicate"], f["object_ref"]) for f in meta.fact_hints}
    assert ("REFERENCES", "concept:Alex") in edges          # wikilink -> concept
    assert ("REFERENCES", "concept:Project X") in edges
    assert ("REFERENCES", "topic:project") in edges         # tag -> topic
    assert ("REFERENCES", "topic:beta") in edges
    for f in meta.fact_hints:
        assert f["subject_ref"] == "concept:Magi Project"
        assert f["subject_type"] == "concept"
        assert f["fact_kind"] == "explicit_fact"
        assert f["origin_mode"] == "source_structured"


def _ctx(mod, vault: Path, last_cursor=None, settings=None):
    return mod_sync_context(mod, vault, last_cursor, settings)


def mod_sync_context(mod, vault: Path, last_cursor, settings):
    # Build a SensorSyncContext with the real SDK dataclass.
    from magi_plugin_sdk.sensors import SensorSyncContext

    class _Paths:
        def plugin_cache_dir(self, plugin_id: str) -> Path:
            return vault
    return SensorSyncContext(
        source_type="obsidian_vault",
        manual=False,
        last_cursor=last_cursor,
        last_success_at=None,
        limit=1000,
        runtime_paths=_Paths(),
        plugin_settings=settings or {},
    )


def test_collect_items_knowledge_tier_skips_search_and_excluded(tmp_path: Path) -> None:
    mod = _load_sensor_module()
    (tmp_path / "Projects").mkdir()
    (tmp_path / "Clippings").mkdir()
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / "Projects" / "A.md").write_text("# A\nbody [[X]]\n", encoding="utf-8")
    (tmp_path / "Clippings" / "C.md").write_text("# C\nclip\n", encoding="utf-8")
    (tmp_path / ".obsidian" / "W.md").write_text("# W\nconfig\n", encoding="utf-8")

    sensor = mod.ObsidianVaultSensor(
        cognition_eligible=True, sensor_suffix="knowledge",
        vault_path=str(tmp_path),
        exclude_folders=[".obsidian"], cognition_exclude_folders=["Clippings"],
    )
    result = asyncio.run(sensor.collect_items(mod_sync_context(mod, tmp_path, None, {})))
    rels = {it["rel_path"] for it in result.items}
    assert rels == {"Projects/A.md"}              # only knowledge-tier note
    assert result.next_cursor is not None


def test_collect_items_incremental_via_cursor(tmp_path: Path) -> None:
    mod = _load_sensor_module()
    (tmp_path / "Projects").mkdir()
    old = tmp_path / "Projects" / "Old.md"
    old.write_text("# Old\nold\n", encoding="utf-8")
    import os
    os.utime(old, (1000.0, 1000.0))  # mtime far in the past

    sensor = mod.ObsidianVaultSensor(
        cognition_eligible=True, sensor_suffix="knowledge",
        vault_path=str(tmp_path), exclude_folders=[], cognition_exclude_folders=[],
    )
    # Cursor newer than the old file -> nothing ingested.
    result = asyncio.run(sensor.collect_items(mod_sync_context(mod, tmp_path, "2000.0", {})))
    assert result.items == []

    # A fresh file (current mtime) is picked up.
    new = tmp_path / "Projects" / "New.md"
    new.write_text("# New\nnew\n", encoding="utf-8")
    result2 = asyncio.run(sensor.collect_items(mod_sync_context(mod, tmp_path, "2000.0", {})))
    assert {it["rel_path"] for it in result2.items} == {"Projects/New.md"}


def test_collect_items_skips_unreadable_note(tmp_path: Path, monkeypatch) -> None:
    """A single failing parse_note must not abort the whole scan."""
    mod = _load_sensor_module()
    (tmp_path / "Projects").mkdir()
    (tmp_path / "Projects" / "Good.md").write_text("# Good\nok\n", encoding="utf-8")
    (tmp_path / "Projects" / "Bad.md").write_text("# Bad\nboom\n", encoding="utf-8")

    real_parse = mod.parse_note

    def flaky(path, root):
        if path.name == "Bad.md":
            raise OSError("locked")
        return real_parse(path, root)

    monkeypatch.setattr(mod, "parse_note", flaky)
    sensor = mod.ObsidianVaultSensor(
        cognition_eligible=True, sensor_suffix="knowledge",
        vault_path=str(tmp_path), exclude_folders=[], cognition_exclude_folders=[],
    )
    result = asyncio.run(sensor.collect_items(mod_sync_context(mod, tmp_path, None, {})))
    assert {it["rel_path"] for it in result.items} == {"Projects/Good.md"}
    assert result.stats["skipped_errors"] == 1
