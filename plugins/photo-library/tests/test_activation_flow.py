"""photo-library must expose an activation_flow with a required source_paths path field."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_plugin_module() -> ModuleType:
    """Load ``plugin.py`` for the hyphenated ``photo-library`` dir.

    The plugin dir is hyphenated (cannot be imported as a package) and has no
    ``__init__.py``, yet ``plugin.py`` uses relative imports
    (``from .sensor import ...``). We synthesize a parent package whose
    ``__path__`` points at the plugin dir, register it in ``sys.modules``, then
    load ``plugin.py`` as a submodule so its relative imports resolve.
    """
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "photo_library_under_test"
    package = ModuleType(package_name)
    package.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.plugin",
        plugin_dir / "plugin.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _photo_sensor_metadata() -> dict:
    photo_plugin = _load_plugin_module()
    inst = photo_plugin.PhotoLibraryPlugin()
    sensors = inst.get_sensors()
    assert sensors, "expected at least one sensor"
    _id, _obj, spec = sensors[0]
    return spec.metadata


def test_photo_library_declares_activation_flow_with_source_paths() -> None:
    meta = _photo_sensor_metadata()
    flow = meta.get("activation_flow")
    assert flow is not None, "photo-library must declare an activation_flow"
    assert flow["enabled_key"] == "sensors.photo_library.enabled"
    assert flow["configured_key"] == "sensors.photo_library.initial_sync_configured"
    sp = next(
        (f for f in flow["fields"] if f["key"] == "sensors.photo_library.source_paths"),
        None,
    )
    assert sp is not None, "activation_flow must include the source_paths field"
    assert sp["type"] == "path" and sp["required"] is True
