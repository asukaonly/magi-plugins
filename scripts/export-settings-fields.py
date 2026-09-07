#!/usr/bin/env python3
"""Export reviewed manifest settings as JSON without importing plugin code.

The manifest is the authoring source of truth. This command validates it through
public SDK models and never rewrites it or infers fields from runtime defaults.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from settings_declarations import read_manifest, settings_catalog

ROOT = Path(__file__).resolve().parents[1]


def export(path: Path) -> dict[str, object]:
    """Return a fresh, validated settings catalog from one manifest."""
    return settings_catalog(read_manifest(path))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="Manifests; defaults to every package")
    parser.add_argument("--check", action="store_true", help="Validate without emitting JSON")
    args = parser.parse_args()
    paths = args.paths or sorted((ROOT / "plugins").glob("*/plugin.toml"))
    catalogs = {str(path): export(path) for path in paths}
    if args.check:
        print(f"Validated settings declarations for {len(catalogs)} packages")
    else:
        print(json.dumps(catalogs, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
