"""Health observations preserve consent, units, identity and factual provenance."""
import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError
from magi_plugin_sdk import SourceChange
from sdk_test_support import bind_test_plugin, load_plugin

MANIFEST = Path(__file__).resolve().parents[1] / "plugins/apple_health/plugin.toml"


def source(consent=True):
    plugin = bind_test_plugin(load_plugin(MANIFEST), settings={"sources": {"apple_health": {"processing_consent": consent}}})
    return plugin.get_sources()[0][1]


def record(**updates):
    return dict(record_id="steps:2026-11-06:Asia/Shanghai", record_type="steps", start=1793894400.0,
                end=1793980800.0, timezone="Asia/Shanghai", value=3500.0, unit="count", **updates)


def test_consent_is_required_for_processing():
    for consent in (False, None, "true"):
        blocked = source(consent)
        with pytest.raises(PermissionError):
            asyncio.run(blocked.fetch_item(record()))
        with pytest.raises(PermissionError):
            asyncio.run(blocked.build_output(record()))


def test_portable_normalization_and_stable_identity():
    receiver = source()
    first = asyncio.run(receiver.build_output(asyncio.run(receiver.fetch_item(record()))))
    changed = record(); changed["value"] = 5000.0
    second = asyncio.run(receiver.build_output(changed))
    assert first.source_item_id == second.source_item_id
    assert first.domain_payload["unit"] == "count"
    assert first.domain_payload["value"] == 3500.0
    assert first.provenance["provider"] == "healthkit"
    assert receiver.source_item_version_fingerprint(record()) != receiver.source_item_version_fingerprint(changed)
    assert receiver.idempotency_key(first) == receiver.idempotency_key(second)
    assert receiver.memory_policy.allow_llm_extraction is False


@pytest.mark.parametrize("changes", [
    {"value": -1.0}, {"value": 2.5}, {"value": float("nan")}, {"unit": "seconds"},
    {"record_type": "blood_glucose"}, {"end": 1.0}, {"file_path": "/private/health.db"},
])
def test_invalid_observations_are_rejected(changes):
    with pytest.raises(ValidationError):
        asyncio.run(source().fetch_item({**record(), **changes}))


@pytest.mark.parametrize("kind", ["sleep", "workout"])
def test_duration_records_remain_factual(kind):
    payload = {**record(), "record_type": kind, "record_id": "healthkit:uuid", "value": 1800.0,
               "unit": "seconds", "category": "deep" if kind == "sleep" else "walking"}
    output = asyncio.run(source().build_output(payload))
    assert output.domain_payload["value"] == 1800
    assert output.activity.action.code == kind
    assert output.domain_payload["category"] == payload["category"]


def test_source_deletion_does_not_require_an_upsert_payload():
    change = SourceChange(object_id="healthkit:uuid", version="delete-1", operation="delete")
    assert change.payload == {}
    assert change.resources == []
