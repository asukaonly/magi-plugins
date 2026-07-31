"""Tests for generated marketplace registry metadata."""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-registry.py"


def _load_build_registry_module():
    spec = importlib.util.spec_from_file_location("build_registry", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_browser_history_plugins_declare_marketplace_display_group() -> None:
    build_registry = _load_build_registry_module()

    expected = {
        "chrome-history": ("Chrome", 10),
        "safari-history": ("Safari", 20),
        "firefox-history": ("Firefox", 30),
        "edge-history": ("Edge", 40),
    }

    for plugin_dir, (member_label, member_order) in expected.items():
        entry = build_registry.build_entry(
            ROOT / "plugins" / plugin_dir, official_ids=set()
        )
        assert entry is not None
        group = entry["display_group"]
        assert group["id"] == "browser_history"
        assert group["name"] == "Browser History"
        assert group["name_i18n"]["zh-CN"] == "浏览器历史"
        assert group["icon"] == "lucide:globe"
        assert group["member_label"] == member_label
        assert group["member_order"] == member_order


def test_suggestion_descriptor_is_generated_in_the_primary_pass() -> None:
    build_registry = _load_build_registry_module()
    entry = build_registry.build_entry(
        ROOT / "plugins" / "chrome-history",
        official_ids=set(),
    )

    assert entry is not None
    assert (
        entry["suggestion_descriptor"]["local_requirements"][0]["check_kind"]
        == "file_exists"
    )


def test_media_and_game_plugins_declare_marketplace_display_groups() -> None:
    build_registry = _load_build_registry_module()

    expected = {
        "netease_music": {
            "id": "listening_history",
            "name": "Listening History",
            "name_zh": "听歌历史",
            "icon": "lucide:music",
            "member_label": "NetEase Cloud Music",
            "member_order": 10,
        },
        "system_media": {
            "id": "listening_history",
            "name": "Listening History",
            "name_zh": "听歌历史",
            "icon": "lucide:music",
            "member_label": "Local Now Playing",
            "member_order": 20,
        },
        "steam_play_history": {
            "id": "game_records",
            "name": "Game Records",
            "name_zh": "游戏记录",
            "icon": "lucide:gamepad-2",
            "member_label": "Steam",
            "member_order": 10,
        },
    }

    for plugin_dir, spec in expected.items():
        entry = build_registry.build_entry(
            ROOT / "plugins" / plugin_dir, official_ids=set()
        )
        assert entry is not None
        group = entry["display_group"]
        assert group["id"] == spec["id"]
        assert group["name"] == spec["name"]
        assert group["name_i18n"]["zh-CN"] == spec["name_zh"]
        assert group["icon"] == spec["icon"]
        assert group["member_label"] == spec["member_label"]
        assert group["member_order"] == spec["member_order"]


def test_each_installable_sensor_package_owns_at_most_one_source() -> None:
    """Independent sources must remain independently installable."""
    for manifest_path in sorted((ROOT / "plugins").glob("*/plugin.toml")):
        plugin = tomllib.loads(manifest_path.read_text())["plugin"]
        if plugin.get("kind", "plugin") == "library":
            continue
        if "sensor" not in plugin.get("contribution_types", []):
            continue
        sensors = (plugin.get("default_settings") or {}).get("sensors") or {}
        assert (
            len(sensors) <= 1
        ), f"{plugin['id']} bundles multiple sources: {sorted(sensors)}"


def test_photo_sources_are_separate_marketplace_plugins() -> None:
    build_registry = _load_build_registry_module()
    expected = {
        "apple-photos": ("Apple Photos", 10, {"photos", "network"}),
        "local-photos": ("Local Photos", 20, {"filesystem_read", "network"}),
    }

    for plugin_dir, (member_label, member_order, capabilities) in expected.items():
        entry = build_registry.build_entry(
            ROOT / "plugins" / plugin_dir,
            official_ids={"apple-photos", "local-photos"},
        )
        assert entry is not None
        assert entry["display_group"]["id"] == "photo_library"
        assert entry["display_group"]["member_label"] == member_label
        assert entry["display_group"]["member_order"] == member_order
        assert {item["capability"] for item in entry["capabilities"]} == capabilities


def test_agent_sources_are_separate_marketplace_plugins() -> None:
    build_registry = _load_build_registry_module()
    expected = {
        "claude-code": ("Claude Code", 10),
        "codex": ("Codex", 20),
    }

    for plugin_dir, (member_label, member_order) in expected.items():
        entry = build_registry.build_entry(
            ROOT / "plugins" / plugin_dir,
            official_ids=set(),
        )
        assert entry is not None
        assert entry["display_group"]["id"] == "agent_history"
        assert entry["display_group"]["member_label"] == member_label
        assert entry["display_group"]["member_order"] == member_order


def test_split_sources_do_not_install_their_siblings() -> None:
    build_registry = _load_build_registry_module()
    entries = {}
    for plugin_dir in sorted((ROOT / "plugins").iterdir()):
        if not plugin_dir.is_dir():
            continue
        entry = build_registry.build_entry(plugin_dir, official_ids=set())
        if entry is not None:
            entries[entry["plugin_id"]] = entry

    def closure(plugin_id: str) -> set[str]:
        resolved: set[str] = set()

        def visit(current_id: str) -> None:
            if current_id in resolved:
                return
            resolved.add(current_id)
            for dependency in entries[current_id].get("depends_on", []):
                visit(dependency)

        visit(plugin_id)
        return resolved

    assert closure("apple-photos") == {"apple-photos", "photo_library_core"}
    assert closure("local-photos") == {"local-photos", "photo_library_core"}
    assert closure("claude-code") == {
        "claude-code",
        "agent_history_core",
    }
    assert closure("codex") == {
        "codex",
        "agent_history_core",
    }


def test_brand_plugins_embed_safe_package_owned_icons() -> None:
    build_registry = _load_build_registry_module()
    plugin_dirs = {
        "apple-photos",
        "chrome-history",
        "claude-code",
        "codex",
        "edge-history",
        "firefox-history",
        "git_activity",
        "github_activity",
        "netease_music",
        "obsidian-vault",
        "safari-history",
        "steam_play_history",
        "telegram",
        "weixin",
    }

    for plugin_dir in sorted(plugin_dirs):
        entry = build_registry.build_entry(
            ROOT / "plugins" / plugin_dir,
            official_ids=set(),
        )
        assert entry is not None
        assert entry["icon"] == "asset:assets/icon.svg"
        prefix, encoded = entry["icon_data"].split(",", 1)
        assert prefix == "data:image/svg+xml;base64"
        assert base64.b64decode(encoded).lstrip().startswith(b"<svg")


def test_every_plugin_declares_a_supported_icon_source() -> None:
    for manifest_path in sorted((ROOT / "plugins").glob("*/plugin.toml")):
        plugin = tomllib.loads(manifest_path.read_text())["plugin"]
        icon = plugin.get("icon", "")
        assert icon.startswith(
            ("asset:", "lucide:")
        ), f"{plugin['id']} uses unsupported icon declaration: {icon}"


def test_generated_registry_and_history_match_frozen_package_metadata() -> None:
    build_registry = _load_build_registry_module()
    registry = json.loads((ROOT / "registry.json").read_text(encoding="utf-8"))
    history = json.loads((ROOT / "version-history.json").read_text(encoding="utf-8"))[
        "packages"
    ]

    assert registry["registry_version"] == "4"
    assert registry["plugins"]
    for entry in registry["plugins"]:
        digest = entry["package_sha256"]
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")
        metadata = build_registry.tracked_plugin_package_metadata(
            ROOT,
            ROOT / entry["path"],
        )
        assert digest == metadata.package_sha256
        assert history[f"{entry['plugin_id']}@{entry['version']}"] == {
            "package_sha256": digest,
            "executable_paths": list(metadata.executable_paths),
        }


def test_asset_icon_rejects_unsafe_svg(tmp_path: Path) -> None:
    build_registry = _load_build_registry_module()
    plugin_dir = tmp_path / "unsafe-plugin"
    asset_dir = plugin_dir / "assets"
    asset_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        """
[plugin]
id = "unsafe-plugin"
name = "Unsafe Plugin"
version = "0.1.0"
icon = "asset:assets/icon.svg"
contribution_types = []
""".strip(),
        encoding="utf-8",
    )
    (asset_dir / "icon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="forbidden <script>"):
        build_registry.build_entry(plugin_dir, official_ids=set())


def test_asset_icon_rejects_path_traversal(tmp_path: Path) -> None:
    build_registry = _load_build_registry_module()

    with pytest.raises(ValueError, match="Invalid plugin icon asset path"):
        build_registry.encode_icon_asset(tmp_path, "asset:../icon.svg")


def test_asset_icon_rejects_symlink(tmp_path: Path) -> None:
    build_registry = _load_build_registry_module()
    target = tmp_path / "target.svg"
    target.write_text("<svg/>", encoding="utf-8")
    link = tmp_path / "icon.svg"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="cannot be a symlink"):
        build_registry.encode_icon_asset(tmp_path, "asset:icon.svg")


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        (
            """
[plugin]
id = "Bad.ID"
name = "Invalid"
version = "1.0.0"
""",
            "plugin.id",
        ),
        (
            """
[plugin]
id = "invalid-kind"
name = "Invalid"
version = "1.0.0"
kind = "not-a-kind"
""",
            "plugin.kind",
        ),
        (
            """
[plugin]
id = "invalid-dependency"
name = "Invalid"
version = "1.0.0"
depends_on = ["../../escape"]
""",
            "depends_on",
        ),
    ],
)
def test_primary_generator_rejects_host_invalid_manifests(
    tmp_path: Path,
    manifest: str,
    message: str,
) -> None:
    build_registry = _load_build_registry_module()
    plugin_dir = tmp_path / "invalid-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.toml").write_text(manifest.strip(), encoding="utf-8")

    with pytest.raises(build_registry.RegistryContractError, match=message):
        build_registry.build_entry(plugin_dir, official_ids=set())
