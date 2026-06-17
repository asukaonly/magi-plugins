from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_reader():
    path = Path(__file__).resolve().parents[1] / "reader.py"
    spec = importlib.util.spec_from_file_location("local_documents_reader_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_markdown_document_extracts_title_tags_links_and_body(tmp_path: Path) -> None:
    reader = _load_reader()
    root = tmp_path
    doc = root / "Projects" / "Magi.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "---\n"
        "title: Magi Roadmap\n"
        "tags: [project, planning]\n"
        "---\n"
        "# Ignored Heading\n"
        "Working with [[Alex]] on [[Project X|the launch]]. Also #beta.\n",
        encoding="utf-8",
    )

    parsed = reader.parse_document(doc, root)

    assert parsed["title"] == "Magi Roadmap"
    assert parsed["rel_path"] == "Projects/Magi.md"
    assert parsed["extension"] == ".md"
    assert parsed["document_kind"] == "markdown"
    assert "Working with" in parsed["body"]
    assert set(parsed["wikilinks"]) == {"Alex", "Project X"}
    assert set(parsed["tags"]) == {"project", "planning", "beta"}
    assert parsed["truncated"] is False
    assert parsed["source_item_id"] == reader.document_id_for_path(doc)


def test_parse_plain_text_uses_first_line_title_and_caps_body(tmp_path: Path) -> None:
    reader = _load_reader()
    doc = tmp_path / "notes" / "scratch.txt"
    doc.parent.mkdir()
    doc.write_text("Launch notes\n\n" + ("detail " * 30), encoding="utf-8")

    parsed = reader.parse_document(doc, tmp_path, max_body_chars=24)

    assert parsed["title"] == "Launch notes"
    assert parsed["document_kind"] == "text"
    assert parsed["body"] == "Launch notes\n\ndetail det"
    assert parsed["truncated"] is True


def test_walk_documents_filters_extensions_case_insensitively(tmp_path: Path) -> None:
    reader = _load_reader()
    (tmp_path / "A.MD").write_text("# A\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("B\n", encoding="utf-8")
    (tmp_path / "c.png").write_bytes(b"png")

    found = {path.name for path in reader.walk_documents(tmp_path, [".md", "txt"])}

    assert found == {"A.MD", "b.txt"}


def test_classify_folder_tiers() -> None:
    reader = _load_reader()
    exclude = [".git", "node_modules", "Private"]
    search_only = ["References", "Archive"]

    assert reader.classify_folder(".git/config.md", exclude, search_only) == "exclude"
    assert reader.classify_folder("Private/Journal.md", exclude, search_only) == "exclude"
    assert reader.classify_folder("References/Paper.txt", exclude, search_only) == "search"
    assert reader.classify_folder("Archive/Old.md", exclude, search_only) == "search"
    assert reader.classify_folder("Projects/Magi.md", exclude, search_only) == "knowledge"
