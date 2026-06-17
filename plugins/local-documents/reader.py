"""Pure parsing helpers for generic local text documents."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Iterable, Iterator

DEFAULT_EXTENSIONS = [".md", ".markdown", ".txt", ".text", ".rst", ".org", ".log"]

_WIKILINK_RE = re.compile(r"\[\[([^\]\n]+?)\]\]")
_INLINE_TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9_][A-Za-z0-9_/\-]*)")
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_ORG_TITLE_RE = re.compile(r"^\s*#\+TITLE:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def normalize_extensions(extensions: Iterable[str] | None) -> list[str]:
    """Return lower-case dotted extensions, preserving input order."""
    values = list(extensions or DEFAULT_EXTENSIONS)
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip().lower()
        if not value:
            continue
        if not value.startswith("."):
            value = f".{value}"
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    return normalized or list(DEFAULT_EXTENSIONS)


def walk_documents(root: Path, extensions: Iterable[str] | None = None) -> Iterator[Path]:
    """Yield files under root whose extension is included."""
    allowed = set(normalize_extensions(extensions))
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in allowed:
            yield path


def document_id_for_path(path: Path) -> str:
    """Stable privacy-light identity for supersession, derived from absolute path."""
    resolved = str(path.expanduser().resolve(strict=False))
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
    return f"file:{digest[:24]}"


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body). Minimal YAML: scalars + inline/block lists."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end].strip("\n")
    body = text[end + 4 :].lstrip("\n")
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
            current_list_key = key
            fm[key] = []
        elif value.startswith("[") and value.endswith("]"):
            items = [item.strip().strip("\"'") for item in value[1:-1].split(",")]
            fm[key] = [item for item in items if item]
        else:
            fm[key] = value.strip("\"'")
    return fm, body


def _normalize_link_target(raw: str) -> str:
    target = raw.split("|", 1)[0]
    target = target.split("#", 1)[0].split("^", 1)[0]
    return target.strip()


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _rst_title(body: str) -> str:
    lines = [line.rstrip() for line in body.splitlines()]
    for index, line in enumerate(lines[:-1]):
        title = line.strip()
        underline = lines[index + 1].strip()
        if title and underline and len(underline) >= len(title):
            chars = set(underline)
            if len(chars) == 1 and chars <= {"=", "-", "~", "^", '"', "'"}:
                return title
    return ""


def _derive_title(body: str, path: Path, extension: str) -> str:
    if extension in {".md", ".markdown"}:
        match = _H1_RE.search(body)
        if match:
            return match.group(1).strip()
    if extension == ".org":
        match = _ORG_TITLE_RE.search(body)
        if match:
            return match.group(1).strip()
    if extension == ".rst":
        title = _rst_title(body)
        if title:
            return title

    first_line = _first_non_empty_line(body)
    if first_line and len(first_line) <= 120:
        return first_line
    return path.stem


def _document_kind(extension: str) -> str:
    if extension in {".md", ".markdown"}:
        return "markdown"
    if extension == ".rst":
        return "rst"
    if extension == ".org":
        return "org"
    return "text"


def parse_document(path: Path, root: Path, *, max_body_chars: int = 50_000) -> dict[str, Any]:
    """Parse one local text document into a normalized item."""
    text = path.read_text(encoding="utf-8", errors="replace")
    frontmatter, raw_body = _split_frontmatter(text)
    extension = path.suffix.lower()
    body = raw_body.strip()

    title = str(frontmatter.get("title") or "").strip()
    if not title:
        title = _derive_title(body, path, extension)

    tags = set(_as_str_list(frontmatter.get("tags")))
    for raw_tag in _INLINE_TAG_RE.findall(body):
        tags.add(raw_tag.strip())

    wikilinks = sorted(
        {_normalize_link_target(match) for match in _WIKILINK_RE.findall(text) if _normalize_link_target(match)}
    )
    capped_body = body
    truncated = False
    if max_body_chars > 0 and len(capped_body) > max_body_chars:
        capped_body = capped_body[:max_body_chars]
        truncated = True

    stat = path.stat()
    root = root.expanduser()
    return {
        "source_item_id": document_id_for_path(path),
        "root_path": str(root),
        "path": str(path),
        "rel_path": path.relative_to(root).as_posix(),
        "title": title,
        "body": capped_body,
        "tags": sorted(tags),
        "wikilinks": wikilinks,
        "frontmatter": frontmatter,
        "extension": extension,
        "document_kind": _document_kind(extension),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "truncated": truncated,
    }


def _path_in_folders(rel_path: str, folders: list[str]) -> bool:
    parts = rel_path.split("/")[:-1]
    folder_set = {folder.strip("/").strip() for folder in folders if folder.strip()}
    return any(part in folder_set for part in parts)


def classify_folder(rel_path: str, exclude_folders: list[str], search_only_folders: list[str]) -> str:
    """Return 'exclude' | 'search' | 'knowledge' for a root-relative document path."""
    if _path_in_folders(rel_path, exclude_folders):
        return "exclude"
    if _path_in_folders(rel_path, search_only_folders):
        return "search"
    return "knowledge"

