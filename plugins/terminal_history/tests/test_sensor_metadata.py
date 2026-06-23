from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import importlib.util
import sys
from pathlib import Path


def _load_sensor_module():
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "terminal_history_metadata_under_test"
    package_spec = importlib.util.spec_from_file_location(
        package_name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    assert package_spec is not None and package_spec.loader is not None
    package = importlib.util.module_from_spec(package_spec)
    sys.modules[package_name] = package
    package_spec.loader.exec_module(package)

    sensor_spec = importlib.util.spec_from_file_location(
        f"{package_name}.sensor",
        plugin_dir / "sensor.py",
    )
    assert sensor_spec is not None and sensor_spec.loader is not None
    module = importlib.util.module_from_spec(sensor_spec)
    sys.modules[sensor_spec.name] = module
    sensor_spec.loader.exec_module(module)
    return module


def test_terminal_history_policy_allows_structured_l2_without_llm() -> None:
    mod = _load_sensor_module()
    sensor = mod.TerminalHistorySensor(reader=None)

    assert sensor.memory_policy.cognition_eligible is True
    assert sensor.memory_policy.allow_llm_extraction is False


def test_extract_metadata_emits_command_tool_fact_hint() -> None:
    mod = _load_sensor_module()
    sensor = mod.TerminalHistorySensor(reader=None)
    meta = asyncio.run(
        sensor.extract_metadata(
            {
                "command": "docker compose ps",
                "executed_at": datetime(2026, 6, 18, 9, 30, tzinfo=timezone.utc),
                "shell": "zsh",
            }
        )
    )

    assert meta.entities == [
        {
            "mention_text": "docker",
            "entity_type": "software",
            "canonical_name_hint": "docker",
        }
    ]
    assert meta.fact_hints == [
        {
            "subject_ref": "user:self",
            "subject_type": "user",
            "predicate": "EXECUTED",
            "object_ref": "software:docker",
            "object_type": "software",
            "fact_kind": "interaction_evidence",
            "origin_mode": "source_structured",
            "confidence": 0.7,
            "observed_at": 1781775000.0,
            "attributes": {"shell": "zsh"},
        }
    ]


def test_extract_metadata_skips_shell_builtins() -> None:
    mod = _load_sensor_module()
    sensor = mod.TerminalHistorySensor(reader=None)
    meta = asyncio.run(sensor.extract_metadata({"command": "cd /tmp"}))

    assert meta.entities == []
    assert meta.fact_hints == []
