"""GitHub Activity sensor behavior."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from magi_plugin_sdk.sensors import SensorSyncContext


def _load_module(name: str):
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "github_activity_under_test"
    package_spec = importlib.util.spec_from_file_location(
        package_name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    package = importlib.util.module_from_spec(package_spec)
    sys.modules[package_name] = package
    package_spec.loader.exec_module(package)
    module_spec = importlib.util.spec_from_file_location(
        f"{package_name}.{name}",
        plugin_dir / f"{name}.py",
    )
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    return module


class _RuntimePaths:
    def plugin_cache_dir(self, plugin_id: str) -> Path:
        return Path("/tmp") / plugin_id


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, int]] = []

    def collect_repository_events(self, repository: str, *, since_iso: str | None, limit: int):
        self.calls.append((repository, since_iso, limit))
        return [
            {
                "source_item_id": f"github:{repository}:pull_request:7",
                "repository": repository,
                "event_kind": "pull_request",
                "title": "Add GitHub sync",
                "summary": "PR #7 opened by asuka: Add GitHub sync",
                "state": "open",
                "actor": "asuka",
                "occurred_at": "2026-06-18T01:02:03Z",
                "url": f"https://github.com/{repository}/pull/7",
                "number": 7,
            }
        ]


def test_collect_items_uses_selected_repositories_and_returns_cursor() -> None:
    sensor_mod = _load_module("sensor")
    fake_client = _FakeClient()
    sensor = sensor_mod.GitHubActivitySensor(
        access_token="token",
        repositories=["acme/app", "https://github.com/acme/lib"],
        client_factory=lambda token: fake_client,
    )
    context = SensorSyncContext(
        source_type="github_activity",
        manual=True,
        last_cursor=None,
        last_success_at=None,
        limit=10,
        runtime_paths=_RuntimePaths(),
        plugin_settings={},
    )

    result = asyncio.run(sensor.collect_items(context))

    assert [call[0] for call in fake_client.calls] == ["acme/app", "acme/lib"]
    assert len(result.items) == 2
    assert result.next_cursor is not None
    assert result.stats["repositories_processed"] == 2


def test_build_output_and_metadata_preserve_project_and_interaction_signal() -> None:
    sensor_mod = _load_module("sensor")
    sensor = sensor_mod.GitHubActivitySensor(access_token="token", repositories=[])
    item = {
        "source_item_id": "github:acme/app:pull_request:7",
        "repository": "acme/app",
        "event_kind": "pull_request",
        "title": "Add GitHub sync",
        "summary": "PR #7 opened by asuka: Add GitHub sync",
        "state": "open",
        "actor": "asuka",
        "occurred_at": "2026-06-18T01:02:03Z",
        "url": "https://github.com/acme/app/pull/7",
        "number": 7,
    }

    output = asyncio.run(sensor.build_output(item))
    metadata = asyncio.run(sensor.extract_metadata(item))

    assert output.source_type == "github_activity"
    assert output.activity.action.code == "pull_request"
    assert output.activity.qualifiers["repository"] == "acme/app"
    assert output.narration.title == "acme/app: Add GitHub sync"
    assert output.provenance["url"].endswith("/pull/7")
    assert metadata.entities == [
        {"mention_text": "acme/app", "entity_type": "software", "canonical_name_hint": "acme/app"}
    ]
    assert metadata.fact_hints[0]["predicate"] == "WORKED_ON"
    assert metadata.fact_hints[0]["object_ref"] == "software:acme/app"
