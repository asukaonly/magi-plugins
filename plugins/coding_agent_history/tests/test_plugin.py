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


def _sensors_by_source():
    mod = _load_plugin_module()
    inst = mod.CodingAgentHistoryPlugin()
    sensors = inst.get_sensors()
    assert sensors, "expected sensors"
    return {spec.metadata["source_type"]: (sensor_id, sensor, spec) for sensor_id, sensor, spec in sensors}


def test_get_sensors_exposes_user_authored_sensor() -> None:
    sensors = _sensors_by_source()
    assert set(sensors) == {"claude_code_agent_history", "codex_agent_history"}

    for source_type, (_sensor_id, sensor, spec) in sensors.items():
        assert spec.sensor_id == f"timeline.{source_type}"
        # THE CRUX: the wired sensor mines the user's own turns ([USER] in L2).
        assert sensor.memory_policy.author_type == "user"
        assert sensor.memory_policy.memory_domain == "user_authored"
        assert sensor.source_type == source_type


def test_agent_history_entries_share_capability_group() -> None:
    sensors = _sensors_by_source()

    claude_meta = sensors["claude_code_agent_history"][2].metadata
    codex_meta = sensors["codex_agent_history"][2].metadata

    assert claude_meta["capability_id"] == codex_meta["capability_id"] == "agent_history"
    assert claude_meta["capability_display_name"] == "Agent History"
    assert claude_meta["entry_id"] == "claude_code"
    assert claude_meta["entry_display_name"] == "Claude Code"
    assert claude_meta["entry_order"] == 10
    assert codex_meta["entry_id"] == "codex"
    assert codex_meta["entry_display_name"] == "Codex"
    assert codex_meta["entry_order"] == 20


def test_agent_history_declares_profile_candidate_l2_profiles() -> None:
    mod = _load_plugin_module()
    plugin = mod.CodingAgentHistoryPlugin()
    profiles = plugin.get_extraction_profiles()

    by_source = {profile.source_types[0]: profile for profile in profiles}
    assert set(by_source) == {"claude_code_agent_history", "codex_agent_history"}

    for source_type, profile in by_source.items():
        assert profile.profile_id == f"source.{source_type}"
        assert profile.allow_graph is True
        assert profile.allow_assertion is True
        assert profile.assertion_mode == "phase2_candidate"
        assert profile.allowed_assertion_families == [
            "identity_profile",
            "communication_profile",
            "preference_profile",
            "routine_profile",
            "state_profile",
        ]
        assert profile.allowed_entity_types == [
            "project",
            "product",
            "software",
            "technology",
            "organization",
            "topic",
            "concept",
            "skill",
            "activity",
        ]
        assert profile.allowed_predicates == [
            "USES",
            "WORKS_WITH",
            "REFERENCES",
            "INTERESTED_IN",
            "CREATES",
            "PLANS_TO",
        ]
        assert "WORKED_ON" not in profile.allowed_predicates
        assert "temporary requests" in (profile.phase2_instructions or "")


def test_metadata_carries_source_type_and_default_settings() -> None:
    sensors = _sensors_by_source()
    defaults = sensors["claude_code_agent_history"][2].metadata["default_settings"]
    assert defaults["enabled"] is False
    assert defaults["source_paths"] == ["~/.claude/projects"]
    assert defaults["initial_sync_lookback_days"] == 30

    defaults = sensors["codex_agent_history"][2].metadata["default_settings"]
    assert defaults["enabled"] is False
    assert defaults["source_paths"] == ["~/.codex"]
    assert defaults["initial_sync_lookback_days"] == 30


def test_activation_flow_has_required_path_field_and_lookback() -> None:
    for source_type, (_id, _sensor, spec) in _sensors_by_source().items():
        flow = spec.metadata.get("activation_flow")
        assert flow is not None, "activation_flow must be present so the install panel renders"
        assert flow["enabled_key"] == f"sensors.{source_type}.enabled"
        assert flow["configured_key"] == f"sensors.{source_type}.initial_sync_configured"

        keys = {f["key"] for f in flow["fields"]}
        assert f"sensors.{source_type}.source_paths" in keys
        assert f"sensors.{source_type}.initial_sync_lookback_days" in keys

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
    assert plugin["name"] == "Agent History"
    assert "sensor" in plugin["contribution_types"]
    # Per-plugin seed defaults the host writes verbatim on first run.
    seeded = plugin["default_settings"]["sensors"]
    assert seeded["claude_code_agent_history"]["enabled"] is False
    assert seeded["claude_code_agent_history"]["source_paths"] == ["~/.claude/projects"]
    assert seeded["codex_agent_history"]["enabled"] is False
    assert seeded["codex_agent_history"]["source_paths"] == ["~/.codex"]
    # A suggestion_descriptor makes the plugin recommendable.
    descriptor = plugin["suggestion_descriptor"]
    assert descriptor["category"]
    assert descriptor["rationale"]["en"] and descriptor["rationale"]["zh"]
    # Reads local transcript files -> must declare filesystem_read.
    capabilities = {c["capability"] for c in plugin["permissions"]["capabilities"]}
    assert "filesystem_read" in capabilities
