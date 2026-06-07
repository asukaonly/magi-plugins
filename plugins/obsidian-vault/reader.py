# reader.py
"""Pure parsing helpers for an Obsidian vault. No SDK imports — keep testable in isolation."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

_WIKILINK_RE = re.compile(r"\[\[([^\]\n]+?)\]\]")
# Inline #tag: must follow start-of-line or whitespace; allow nested a/b and -, _.
_INLINE_TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9_][A-Za-z0-9_/\-]*)")
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def walk_markdown(vault_root: Path) -> Iterator[Path]:
    """Yield every .md file under the vault, skipping nothing (callers filter folders)."""
    yield from (p for p in vault_root.rglob("*.md") if p.is_file())


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body). Minimal YAML — scalars + inline/block lists only."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    fm: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if current_list_key and line.lstrip().startswith("- "):
            fm.setdefault(current_list_key, [])
            fm[current_list_key].append(line.lstrip()[2:].strip().strip("\"'"))
            continue
        current_list_key = None
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "":
            current_list_key = key  # block list follows on next lines
            fm[key] = []
        elif value.startswith("[") and value.endswith("]"):
            items = [v.strip().strip("\"'") for v in value[1:-1].split(",")]
            fm[key] = [v for v in items if v]
        else:
            fm[key] = value.strip("\"'")
    return fm, body


def _normalize_link_target(raw: str) -> str:
    """`[[Target|alias]]` -> `Target`; strip `#section` and `^block` refs and whitespace."""
    target = raw.split("|", 1)[0]
    target = target.split("#", 1)[0].split("^", 1)[0]
    return target.strip()


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def parse_note(path: Path, vault_root: Path) -> dict[str, Any]:
    """Parse one markdown note into a normalized dict."""
    text = path.read_text(encoding="utf-8", errors="replace")
    fm, body = _split_frontmatter(text)

    title = str(fm.get("title") or "").strip()
    if not title:
        m = _H1_RE.search(body)
        title = m.group(1).strip() if m else path.stem

    wikilinks = sorted({_normalize_link_target(m) for m in _WIKILINK_RE.findall(text) if _normalize_link_target(m)})

    tags = set(_as_str_list(fm.get("tags")))
    for raw_tag in _INLINE_TAG_RE.findall(body):
        tags.add(raw_tag.strip())

    rel_path = path.relative_to(vault_root).as_posix()
    uid = str(fm.get("uid") or fm.get("id") or "").strip()

    return {
        "rel_path": rel_path,
        "uid": uid,
        "title": title,
        "body": body.strip(),
        "tags": sorted(tags),
        "wikilinks": wikilinks,
        "aliases": _as_str_list(fm.get("aliases")),
        "frontmatter": fm,
        "mtime": path.stat().st_mtime,
    }


def _path_in_folders(rel_path: str, folders: list[str]) -> bool:
    """True if rel_path is inside any of the given top-or-nested folder names."""
    parts = rel_path.split("/")[:-1]  # directory segments only
    folder_set = {f.strip("/").strip() for f in folders if f.strip()}
    return any(seg in folder_set for seg in parts)


def classify_folder(rel_path: str, exclude_folders: list[str], search_only_folders: list[str]) -> str:
    """Return 'exclude' | 'search' | 'knowledge' for a vault-relative note path."""
    if _path_in_folders(rel_path, exclude_folders):
        return "exclude"
    if _path_in_folders(rel_path, search_only_folders):
        return "search"
    return "knowledge"
