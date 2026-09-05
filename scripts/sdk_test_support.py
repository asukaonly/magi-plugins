"""Host doubles for SDK-only plugin contract and package tests."""
from __future__ import annotations

import atexit
import importlib.util
import sys
import tempfile
import tomllib
from pathlib import Path

from magi_plugin_sdk import PluginManifest
from magi_plugin_sdk.context import PluginContext
from magi_plugin_sdk.runtime import PluginConnection

_TEMPORARIES: list[tempfile.TemporaryDirectory] = []
_CREDENTIAL_PORTS: dict[str, MemoryCredentials] = {}


class MemoryCredentials:
    """One connection's credential port; callers cannot select another scope."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def private_dir() -> Path:
    temporary = tempfile.TemporaryDirectory(prefix="plugin-sdk-test-")
    _TEMPORARIES.append(temporary)
    return Path(temporary.name).resolve()


def bind_test_plugin(plugin, *, connection_id: str = "test-connection", settings: dict | None = None):
    """Configure a real plugin through the production SDK binding contract."""
    path = plugin.plugin_dir / "plugin.toml"
    metadata = tomllib.loads(path.read_text())["plugin"]
    manifest = PluginManifest.model_validate({**metadata, "manifest_path": str(path)})
    connection = PluginConnection(
        connection_id=connection_id, plugin_id=manifest.plugin_id,
        display_name=manifest.name, settings=settings or {}, enabled=True,
    )
    root = private_dir()
    plugin.configure(manifest=manifest, connection=connection, context=PluginContext(
        connection=connection, state_dir=root / "state", resources_dir=root / "resources",
        credentials=MemoryCredentials(),
    ))
    return plugin


@atexit.register
def _cleanup() -> None:
    for temporary in _TEMPORARIES:
        temporary.cleanup()


def credentials_for_path(path) -> MemoryCredentials:
    """Model a host credential scope shared by instances of one test connection."""
    key = str(Path(path).resolve())
    if key not in _CREDENTIAL_PORTS:
        _CREDENTIAL_PORTS[key] = MemoryCredentials()
    return _CREDENTIAL_PORTS[key]


def load_plugin(path: Path):
    meta = tomllib.loads(path.read_text())["plugin"]
    name = f"sdk_conformance_{path.parent.name.replace('-', '_')}"
    package_spec = importlib.util.spec_from_file_location(
        name, path.parent / "__init__.py", submodule_search_locations=[str(path.parent)],
    )
    package = importlib.util.module_from_spec(package_spec)
    sys.modules[name] = package
    if (path.parent / "__init__.py").exists():
        package_spec.loader.exec_module(package)
    spec = importlib.util.spec_from_file_location(f"{name}.{meta['entry_module']}", path.parent / f"{meta['entry_module']}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return getattr(module, meta["entry_class"])()
