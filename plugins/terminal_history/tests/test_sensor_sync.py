from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType


def _load_sensor_module() -> ModuleType:
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "terminal_history_sensor_sync_under_test"
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
    shell = "zsh"

    def read_commands(self, *, limit: int, **kwargs):
        from terminal_history_sensor_sync_under_test.types import TerminalCommand

        return [
            TerminalCommand(
                command=f"echo {idx}",
                executed_at=datetime.fromtimestamp(1_710_000_000 + idx, tz=timezone.utc),
                shell="zsh",
                history_line=idx,
                raw_line=f"echo {idx}",
            )
            for idx in range(1, limit + 1)
        ]


def test_terminal_history_marks_has_more_when_limit_is_full() -> None:
    mod = _load_sensor_module()
    sensor = mod.TerminalHistorySensor(reader=_Reader())

    from magi_plugin_sdk.sensors import SensorSyncContext

    result = asyncio.run(
        sensor.collect_items(
            SensorSyncContext(
                source_type="terminal_history",
                manual=True,
                last_cursor="1710000000",
                last_success_at=None,
                limit=2,
                runtime_paths=_RuntimePaths(),
                plugin_settings={"sensors": {"terminal_history": {}}},
            )
        )
    )

    assert len(result.items) == 2
    assert result.stats["has_more"] is True
