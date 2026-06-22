from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType


def _load_sensor_module() -> ModuleType:
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "git_activity_sensor_sync_under_test"
    if package_name not in sys.modules:
        package_spec = importlib.util.spec_from_file_location(
            package_name,
            plugin_dir / "__init__.py",
            submodule_search_locations=[str(plugin_dir)],
        )
        assert package_spec is not None and package_spec.loader is not None
        package = importlib.util.module_from_spec(package_spec)
        sys.modules[package_name] = package
        package_spec.loader.exec_module(package)

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.sensor",
        plugin_dir / "sensor.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _RuntimePaths:
    def plugin_cache_dir(self, plugin_id: str) -> Path:
        return Path("/tmp") / plugin_id


class _Reader:
    def read_activities(self, *, limit: int, **kwargs):
        from git_activity_sensor_sync_under_test.types import GitActivity

        return [
            GitActivity(
                repo_path="/repo",
                activity_type="commit",
                old_sha="0" * 40,
                new_sha=str(idx) * 40,
                message=f"commit: change {idx}",
                author="Asuka <asuka@example.com>",
                timestamp=datetime.fromtimestamp(1_710_000_000 + idx, tz=timezone.utc),
                raw_line="",
            )
            for idx in range(1, limit + 1)
        ]


def test_git_activity_marks_has_more_when_repo_limit_is_full() -> None:
    mod = _load_sensor_module()
    mod.is_git_repo = lambda _path: True
    sensor = mod.GitActivitySensor(repos=["/repo"])
    sensor._readers["/repo"] = _Reader()

    from magi_plugin_sdk.sensors import SensorSyncContext

    result = asyncio.run(
        sensor.collect_items(
            SensorSyncContext(
                source_type="git_activity",
                manual=True,
                last_cursor=None,
                last_success_at=None,
                limit=2,
                runtime_paths=_RuntimePaths(),
                plugin_settings={"sensors": {"git_activity": {"repos": ["/repo"]}}},
            )
        )
    )

    assert result.stats["raw_activity_count"] == 2
    assert result.stats["has_more"] is True
