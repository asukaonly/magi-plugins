"""git_activity structured-only adoption: deterministic COMMITTED edge + skip-LLM flag."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_sensor() -> ModuleType:
    pd = Path(__file__).resolve().parents[1]
    pkg = "git_activity_under_test"
    spec = importlib.util.spec_from_file_location(
        pkg, pd / "__init__.py", submodule_search_locations=[str(pd)]
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules[pkg] = m
    spec.loader.exec_module(m)
    s = importlib.util.spec_from_file_location(f"{pkg}.sensor", pd / "sensor.py")
    mod = importlib.util.module_from_spec(s)
    sys.modules[s.name] = mod
    s.loader.exec_module(mod)
    return mod


def test_structured_only_flag_is_set() -> None:
    sensor = _load_sensor().GitActivitySensor(repos=[])
    assert sensor.memory_policy.allow_llm_extraction is False


def test_extract_metadata_emits_committed_edge() -> None:
    sensor = _load_sensor().GitActivitySensor(repos=[])
    meta = asyncio.run(sensor.extract_metadata({"repo_path": "/home/u/code/magi"}))
    assert meta.entities == [
        {"mention_text": "magi", "entity_type": "software", "canonical_name_hint": "magi"}
    ]
    assert len(meta.fact_hints) == 1
    f = meta.fact_hints[0]
    assert (f["predicate"], f["object_ref"], f["object_type"]) == ("COMMITTED", "software:magi", "software")
    assert f["subject_ref"] == "user:self"
    assert f["fact_kind"] == "interaction_evidence"
    assert f["origin_mode"] == "source_structured"


def test_extract_metadata_empty_without_repo() -> None:
    sensor = _load_sensor().GitActivitySensor(repos=[])
    meta = asyncio.run(sensor.extract_metadata({}))
    assert meta.entities == [] and meta.fact_hints == []
