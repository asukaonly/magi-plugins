#!/usr/bin/env python3
"""Export public authoring declarations into reviewed, pre-execution manifests.

Run only against trusted source during development. Runtime discovery reads the
resulting TOML and never imports plugins to discover their settings schema.
"""
from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
from magi_plugin_sdk import ExtensionFieldSpec, PluginSettingsActionSpec, PluginSettingsResourceSpec, SettingsUIBlockSpec
from sdk_test_support import bind_test_plugin, load_plugin


def toml(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(json.dumps(str(k)) + " = " + toml(v) for k, v in value.items() if v is not None) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(toml(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def flatten(value: dict, prefix: str = ""):
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            yield from flatten(item, name)
        else:
            yield name, item


def implicit_field(key: str, value):
    kind = "switch" if isinstance(value, bool) else "number" if isinstance(value, (float, int)) else "tags" if isinstance(value, (list, tuple)) else "input"
    if key.endswith(("bot_token", "access_token", "webhook_secret", "lastfm_api_key", "account")):
        kind = "secret"
        value = None
    return ExtensionFieldSpec(key=key, type=kind, label=key.rsplit(".", 1)[-1].replace("_", " ").title(), default=value, section="advanced")


def export(path: Path) -> int:
    text = path.read_text()
    # Exported activation and field declarations are always the final sections.
    text = re.split(r"\n\[plugin.activation_flow\]|\n\[\[plugin.settings_fields\]\]", text, maxsplit=1)[0]
    text = re.sub(r"^settings_fields = \[\]\n", "", text, flags=re.M)
    text = re.sub(r"^settings_(actions|resources|ui_blocks) = .*\n", "", text, flags=re.M)
    meta = tomllib.loads(text)["plugin"]
    fields = {}
    primary_activation = None
    catalog_types = {"settings_actions": (PluginSettingsActionSpec, "action_id"), "settings_resources": (PluginSettingsResourceSpec, "resource_name"), "settings_ui_blocks": (SettingsUIBlockSpec, "block_id")}
    catalogs = {key: {} for key in catalog_types}

    def collect_catalog(key, entries):
        model, identity = catalog_types[key]
        for entry in entries:
            data = (entry if isinstance(entry, model) else model.model_validate(entry)).model_dump(exclude_none=True)
            previous = catalogs[key].setdefault(data[identity], data)
            if previous != data:
                raise ValueError(f"Conflicting {key} declaration: {data[identity]}")

    defaults = dict(flatten(meta.get("default_settings", {})))
    if meta.get("kind") != "library":
        plugin = bind_test_plugin(load_plugin(path))
        for key in catalog_types:
            method = getattr(plugin, f"get_{key}", None)
            if method is not None:
                collect_catalog(key, method())
        for field in plugin.get_channel_fields():
            fields[field.key] = field
        for _, sensor, spec in plugin.get_sensors():
            for key in catalog_types:
                collect_catalog(key, spec.metadata.get(key, []))
            for field in spec.fields:
                fields.setdefault(field.key, field)
            flow = spec.metadata.get("activation_flow", {})
            if flow and primary_activation is None:
                # The first registered source is the package's primary activation.
                primary_activation = flow
            for field in flow.get("fields", []):
                field = ExtensionFieldSpec.model_validate(field)
                fields.setdefault(field.key, field)
            for flag in ("configured_key", "enabled_key"):
                if flow.get(flag):
                    defaults.setdefault(flow[flag], False)
            prefix = next((f.key.rsplit(".", 1)[0] for f in spec.fields if f.key.startswith("sensors.")), f"sensors.{sensor.source_type}")
            defaults.update({f"{prefix}.{key}": value for key, value in spec.metadata.get("default_settings", {}).items()})
        # Runtime-only channel controls absent from their descriptive form.
        hidden = {
            "telegram": {"webhook_secret": "", "magi_user_id": "default", "max_message_length": 4096},
            "weixin": {"account": "", "base_url": "https://ilinkai.weixin.qq.com", "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c", "bot_type": "3", "ilink_app_id": "bot", "route_tag": "", "max_message_length": 4000, "poll_timeout_ms": 35000, "request_timeout_ms": 15000},
            "github-activity": {"sensors.github_activity.client_id": "", "sensors.github_activity.access_token": ""},
            "steam-play-history": {"sensors.steam_play_history.account_id": "auto", "sensors.steam_play_history.excluded_appids": [], "sensors.steam_play_history.excluded_keywords": []},
            "git-activity": {"sensors.git_activity.session_window_minutes": 30, "sensors.git_activity.max_messages_per_session": 5},
            "terminal-history": {"sensors.terminal_history.dedup_window_seconds": 60},
            "local-photos": {"locale": ""},
            "apple-photos": {"locale": ""},
        }.get(meta["id"], {})
        defaults.update(hidden)
        if meta["id"] in {"apple-photos", "local-photos"}:
            source = "photo_library_apple_photos" if meta["id"] == "apple-photos" else "photo_library_directory"
            for key, value in {"initial_sync_policy": "", "initial_sync_start_date": "", "initial_sync_end_date": ""}.items():
                defaults.setdefault(f"sensors.{source}.{key}", value)
    for key, value in defaults.items():
        fields.setdefault(key, implicit_field(key, value))
    catalog_text = "".join(f"{key} = {toml(list(entries.values()))}\n" for key, entries in catalogs.items())
    text = text.replace("[plugin]\n", "[plugin]\n" + catalog_text, 1)
    if not fields:
        path.write_text(text.replace("[plugin]\n", "[plugin]\nsettings_fields = []\n", 1).rstrip() + "\n")
        return 0
    blocks = []
    if primary_activation:
        blocks.append("\n[plugin.activation_flow]\n" + "\n".join(
            f"{name} = {toml(value)}" for name, value in primary_activation.items() if value is not None
        ))
    for key, field in sorted(fields.items()):
        data = field.model_dump(exclude_none=True)
        blocks.append("\n[[plugin.settings_fields]]\n" + "\n".join(f"{name} = {toml(value)}" for name, value in data.items() if value != []))
    path.write_text(text.rstrip() + "\n" + "\n".join(blocks) + "\n")
    return len(fields)


if __name__ == "__main__":
    for path in sorted((ROOT / "plugins").glob("*/plugin.toml")):
        print(f"{path.parent.name}: {export(path)} fields")
