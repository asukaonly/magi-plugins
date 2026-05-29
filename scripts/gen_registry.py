#!/usr/bin/env python3
"""Regenerate registry.json's suggestion_descriptor fields from each plugin's plugin.toml.

Idempotent: for every entry in registry.json, if plugins/<path>/plugin.toml has a
[plugin.suggestion_descriptor], copy it onto the entry as "suggestion_descriptor";
otherwise ensure the field is absent. Preserves all other entry fields + ordering.
"""
import json
import pathlib
import sys

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "registry.json"


def main() -> int:
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    changed = 0
    for entry in data.get("plugins", []):
        rel = entry.get("path")
        if not rel:
            continue
        toml_path = ROOT / rel / "plugin.toml"
        desc = None
        if toml_path.is_file():
            toml = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            desc = (toml.get("plugin", {}) or {}).get("suggestion_descriptor")
        if desc is not None:
            if entry.get("suggestion_descriptor") != desc:
                entry["suggestion_descriptor"] = desc
                changed += 1
        elif "suggestion_descriptor" in entry:
            del entry["suggestion_descriptor"]
            changed += 1
    REGISTRY.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"updated {changed} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
