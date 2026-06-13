"""Tests for the coding-agent history plugin manifest + activation flow.

Loads ``plugin.py`` via the repo's synthesized-loader convention (mirrors
``tests/test_sensor.py``): a synthetic parent package whose ``__path__`` points at
the plugin dir so ``plugin.py``'s package-relative imports (``from .sensor import
CodingAgentHistorySensor``) resolve against the worktree copy without putting
``plugins/`` on sys.path.

The crux re-asserted here (``test_get_sensors_exposes_user_authored_sensor``): the
sensor returned by ``get_sensors`` carries ``author_type="user"`` so L2 mines the
ingested turns as the user's own facts -- mis-wiring the plugin would fail this.
"""
from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path
from types import ModuleType

_PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_plugin_module() -> ModuleType:
    pkg_name = "coding_agent_history_under_test"
    if pkg_name not in sys.modules:
        pkg_spec = importlib.util.spec_from_file_location(
            pkg_name, _PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(_PLUGIN_DIR)]
        )
        assert pkg_spec is not None and pkg_spec.loader is not None
        package = importlib.util.module_from_spec(pkg_spec)
        sys.modules[pkg_name] = package
        pkg_spec.loader.exec_module(package)

    mod_name = f"{pkg_name}.plugin"
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _sensor_and_spec():
    mod = _load_plugin_module()
    inst = mod.CodingAgentHistoryPlugin()
    sensors = inst.get_sensors()
    assert sensors, "expected one sensor"
    sensor_id, sensor, spec = sensors[0]
    return sensor_id, sensor, spec


def test_get_sensors_exposes_user_authored_sensor() -> None:
    sensor_id, sensor, spec = _sensor_and_spec()
    assert sensor_id == "timeline.coding_agent_history"
    assert spec.sensor_id == "timeline.coding_agent_history"
    # THE CRUX: the wired sensor mines the user's own turns ([USER] in L2).
    assert sensor.memory_policy.author_type == "user"
    assert sensor.memory_policy.memory_domain == "user_authored"
    assert sensor.source_type == "coding_agent_history"


def test_metadata_carries_source_type_and_default_settings() -> None:
    _id, _sensor, spec = _sensor_and_spec()
    meta = spec.metadata
    assert meta["source_type"] == "coding_agent_history"
    defaults = meta["default_settings"]
    assert defaults["enabled"] is False
    # Default scan roots are the two v1 agent layouts.
    assert defaults["source_paths"] == ["~/.claude/projects", "~/.codex"]
    assert defaults["initial_sync_lookback_days"] == 30


def test_activation_flow_has_required_path_field_and_lookback() -> None:
    _id, _sensor, spec = _sensor_and_spec()
    flow = spec.metadata.get("activation_flow")
    assert flow is not None, "activation_flow must be present so the install panel renders"
    assert flow["enabled_key"] == "sensors.coding_agent_history.enabled"
    assert flow["configured_key"] == "sensors.coding_agent_history.initial_sync_configured"

    keys = {f["key"] for f in flow["fields"]}
    assert "sensors.coding_agent_history.source_paths" in keys
    assert "sensors.coding_agent_history.initial_sync_lookback_days" in keys

    source_paths = next(f for f in flow["fields"] if f["key"].endswith(".source_paths"))
    assert source_paths["type"] == "path"
    assert source_paths["required"] is True

    lookback = next(f for f in flow["fields"] if f["key"].endswith(".initial_sync_lookback_days"))
    assert lookback["type"] == "number"


def test_manifest_declares_sensor_and_suggestion_descriptor() -> None:
    with (_PLUGIN_DIR / "plugin.toml").open("rb") as handle:
        manifest = tomllib.load(handle)
    plugin = manifest["plugin"]
    assert plugin["id"] == "coding_agent_history"
    assert plugin["entry_class"] == "CodingAgentHistoryPlugin"
    assert "sensor" in plugin["contribution_types"]
    # Per-plugin seed defaults the host writes verbatim on first run.
    seeded = plugin["default_settings"]["sensors"]["coding_agent_history"]
    assert seeded["enabled"] is False
    assert seeded["source_paths"] == ["~/.claude/projects", "~/.codex"]
    # A suggestion_descriptor makes the plugin recommendable.
    descriptor = plugin["suggestion_descriptor"]
    assert descriptor["category"]
    assert descriptor["rationale"]["en"] and descriptor["rationale"]["zh"]
    # Reads local transcript files -> must declare filesystem_read.
    capabilities = {c["capability"] for c in plugin["permissions"]["capabilities"]}
    assert "filesystem_read" in capabilities
