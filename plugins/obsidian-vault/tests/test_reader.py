# tests/test_reader.py
from __future__ import annotations
from pathlib import Path
import importlib.util


def _load_reader():
    path = Path(__file__).resolve().parents[1] / "reader.py"
    spec = importlib.util.spec_from_file_location("obsidian_reader_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_note_extracts_title_body_tags_links(tmp_path: Path) -> None:
    reader = _load_reader()
    vault = tmp_path
    note = vault / "Projects" / "Magi.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\n"
        "title: Magi Project\n"
        "aliases: [Magi, MagiApp]\n"
        "tags: [project, ai]\n"
        "---\n"
        "# Magi Project\n"
        "Working with [[Alex]] on [[Project X|the launch]]. Also #beta work.\n",
        encoding="utf-8",
    )
    parsed = reader.parse_note(note, vault)
    assert parsed["title"] == "Magi Project"
    assert parsed["rel_path"] == "Projects/Magi.md"
    assert "Working with" in parsed["body"]
    assert set(parsed["wikilinks"]) == {"Alex", "Project X"}
    assert set(parsed["aliases"]) == {"Magi", "MagiApp"}
    assert set(parsed["tags"]) == {"project", "ai", "beta"}
    assert parsed["mtime"] == note.stat().st_mtime


def test_parse_note_title_falls_back_to_h1_then_filename(tmp_path: Path) -> None:
    reader = _load_reader()
    note = tmp_path / "Note Without Frontmatter.md"
    note.write_text("# Heading Title\nbody\n", encoding="utf-8")
    assert reader.parse_note(note, tmp_path)["title"] == "Heading Title"

    note2 = tmp_path / "Bare.md"
    note2.write_text("just text, no heading\n", encoding="utf-8")
    assert reader.parse_note(note2, tmp_path)["title"] == "Bare"


def test_classify_folder_tiers() -> None:
    reader = _load_reader()
    exclude = [".obsidian", "Templates"]
    search_only = ["Clippings", "References"]
    # exclude wins over everything
    assert reader.classify_folder(".obsidian/workspace.md", exclude, search_only) == "exclude"
    assert reader.classify_folder("Templates/Daily.md", exclude, search_only) == "exclude"
    # search-only folders
    assert reader.classify_folder("Clippings/some-article.md", exclude, search_only) == "search"
    assert reader.classify_folder("References/paper.md", exclude, search_only) == "search"
    # everything else is knowledge
    assert reader.classify_folder("Projects/Magi.md", exclude, search_only) == "knowledge"
    assert reader.classify_folder("Daily/2026-06-07.md", exclude, search_only) == "knowledge"
