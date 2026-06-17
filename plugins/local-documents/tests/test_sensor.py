from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType


def _load_sensor_module() -> ModuleType:
    plugin_dir = Path(__file__).resolve().parents[1]
    pkg_name = "local_documents_under_test"
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


def _ctx(root_paths: list[str], last_cursor: str | None = None, settings: dict | None = None):
    from magi_plugin_sdk.sensors import SensorSyncContext

    class _Paths:
        def plugin_cache_dir(self, plugin_id: str) -> Path:
            return Path(root_paths[0]) if root_paths else Path(".")

    return SensorSyncContext(
        source_type="local_documents",
        manual=False,
        last_cursor=last_cursor,
        last_success_at=None,
        limit=1000,
        runtime_paths=_Paths(),
        plugin_settings=settings
        or {
            "sensors": {
                "local_documents": {
                    "root_paths": root_paths,
                    "include_extensions": [".md", ".txt", ".rst"],
                    "exclude_folders": [".git", "node_modules"],
                    "cognition_exclude_folders": ["References"],
                    "max_file_bytes": 100_000,
                    "max_body_chars": 20_000,
                }
            }
        },
    )


def _sample_item(mod) -> dict:
    path = Path("/tmp/notes/Projects/Magi.md")
    return {
        "source_item_id": mod.document_id_for_path(path),
        "root_path": "/tmp/notes",
        "rel_path": "Projects/Magi.md",
        "path": str(path),
        "title": "Magi Project",
        "body": "First line summary.\n\nMore project detail.",
        "tags": ["project", "beta"],
        "wikilinks": ["Alex"],
        "extension": ".md",
        "document_kind": "markdown",
        "size": 120,
        "mtime": 1781000000.0,
        "truncated": False,
    }


def test_collect_items_knowledge_tier_scans_multiple_roots_and_filters(tmp_path: Path) -> None:
    mod = _load_sensor_module()
    notes = tmp_path / "notes"
    docs = tmp_path / "docs"
    (notes / "Projects").mkdir(parents=True)
    (notes / "References").mkdir()
    (notes / "node_modules").mkdir()
    (docs / "Specs").mkdir(parents=True)
    (notes / "Projects" / "A.md").write_text("# A\nbody [[X]]\n", encoding="utf-8")
    (notes / "References" / "Paper.txt").write_text("Paper\ncitation\n", encoding="utf-8")
    (notes / "node_modules" / "Junk.md").write_text("# Junk\n", encoding="utf-8")
    (docs / "Specs" / "B.rst").write_text("Spec Title\n==========\n", encoding="utf-8")
    (docs / "image.png").write_bytes(b"png")

    sensor = mod.LocalDocumentsSensor(
        cognition_eligible=True,
        sensor_suffix="knowledge",
        root_paths=[str(notes), str(docs)],
        exclude_folders=["node_modules"],
        cognition_exclude_folders=["References"],
        include_extensions=[".md", ".txt", ".rst"],
    )

    result = asyncio.run(sensor.collect_items(_ctx([str(notes), str(docs)])))

    rels = {(item["root_path"], item["rel_path"]) for item in result.items}
    assert rels == {
        (str(notes), "Projects/A.md"),
        (str(docs), "Specs/B.rst"),
    }
    assert result.next_cursor is not None
    assert result.stats["skipped_oversized"] == 0


def test_collect_items_search_tier_only_reads_search_folders(tmp_path: Path) -> None:
    mod = _load_sensor_module()
    (tmp_path / "References").mkdir()
    (tmp_path / "Projects").mkdir()
    (tmp_path / "References" / "Paper.txt").write_text("Paper\n", encoding="utf-8")
    (tmp_path / "Projects" / "A.md").write_text("# A\n", encoding="utf-8")

    sensor = mod.LocalDocumentsSensor(
        cognition_eligible=False,
        sensor_suffix="search",
        root_paths=[str(tmp_path)],
        exclude_folders=[],
        cognition_exclude_folders=["References"],
        include_extensions=[".md", ".txt"],
    )

    result = asyncio.run(sensor.collect_items(_ctx([str(tmp_path)])))

    assert [item["rel_path"] for item in result.items] == ["References/Paper.txt"]


def test_collect_items_incremental_via_cursor(tmp_path: Path) -> None:
    mod = _load_sensor_module()
    old = tmp_path / "old.md"
    old.write_text("# Old\n", encoding="utf-8")
    os.utime(old, (1000.0, 1000.0))

    sensor = mod.LocalDocumentsSensor(
        cognition_eligible=True,
        sensor_suffix="knowledge",
        root_paths=[str(tmp_path)],
        exclude_folders=[],
        cognition_exclude_folders=[],
        include_extensions=[".md"],
    )

    result = asyncio.run(sensor.collect_items(_ctx([str(tmp_path)], last_cursor="2000.0")))
    assert result.items == []

    new = tmp_path / "new.md"
    new.write_text("# New\n", encoding="utf-8")
    result2 = asyncio.run(sensor.collect_items(_ctx([str(tmp_path)], last_cursor="2000.0")))
    assert [item["rel_path"] for item in result2.items] == ["new.md"]


def test_build_output_maps_document_to_l1_fields() -> None:
    mod = _load_sensor_module()
    sensor = mod.LocalDocumentsSensor(cognition_eligible=True, sensor_suffix="knowledge")
    out = asyncio.run(sensor.build_output(_sample_item(mod)))

    assert out.source_type == "local_documents"
    assert out.source_item_id == _sample_item(mod)["source_item_id"]
    assert out.occurred_at == 1781000000.0
    assert out.narration.title == "Magi Project"
    assert out.narration.body == "First line summary."
    assert out.pinned_payload == _sample_item(mod)["body"]
    assert out.activity.source.code == "local_documents"
    assert out.activity.object is not None and out.activity.object.code == "document"
    assert out.activity.qualifiers["extension"] == ".md"
    assert out.activity.qualifiers["word_count"] == 6
    assert set(out.tags) == {"project", "beta"}


def test_extract_metadata_emits_structured_tags_and_links() -> None:
    mod = _load_sensor_module()
    sensor = mod.LocalDocumentsSensor(cognition_eligible=True, sensor_suffix="knowledge")
    meta = asyncio.run(sensor.extract_metadata(_sample_item(mod)))

    by_mention = {entity["mention_text"]: entity for entity in meta.entities}
    assert by_mention["Magi Project"]["entity_type"] == "concept"
    assert by_mention["Alex"]["entity_type"] == "concept"
    assert by_mention["beta"]["entity_type"] == "topic"
    edges = {(fact["predicate"], fact["object_ref"]) for fact in meta.fact_hints}
    assert ("REFERENCES", "concept:Alex") in edges
    assert ("REFERENCES", "topic:project") in edges
    assert meta.relation_candidates == []
