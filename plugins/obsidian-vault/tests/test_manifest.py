from __future__ import annotations
import tomllib
from pathlib import Path


def test_manifest_is_valid_sensor_plugin() -> None:
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "plugin.toml").read_text(encoding="utf-8"))
    plugin = data["plugin"]
    assert plugin["id"] == "obsidian-vault"
    assert plugin["entry_module"] == "plugin"
    assert plugin["entry_class"] == "ObsidianVaultPlugin"
    assert "sensor" in plugin["contribution_types"]
    # Opt-in by default (privacy-forward), like screenshot_timeline.
    assert plugin["default_settings"]["sensors"]["obsidian_vault"]["enabled"] is False
    # Local-only must be declared so the host can render a privacy badge.
    assert plugin["suggestion_descriptor"]["data_locality"] == "local_only"
    # filesystem_read capability must be declared.
    caps = [c["capability"] for c in plugin["permissions"]["capabilities"]]
    assert "filesystem_read" in caps
