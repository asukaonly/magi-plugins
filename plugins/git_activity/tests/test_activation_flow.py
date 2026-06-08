"""git_activity's activation_flow must include the mandatory repos path field."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_plugin_module() -> ModuleType:
    """Load ``plugin.py`` for the ``git_activity`` dir.

    ``plugin.py`` uses relative imports (``from .sensor import ...``), so we
    synthesize a parent package whose ``__path__`` points at the plugin dir,
    register it in ``sys.modules``, then load ``plugin.py`` as a submodule so
    its relative imports resolve.
    """
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "git_activity_under_test"
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


def test_git_activity_flow_includes_repos() -> None:
    git_plugin = _load_plugin_module()
    flow = git_plugin._activation_flow("sensors.git_activity")
    keys = [f.key for f in flow.fields]
    assert "sensors.git_activity.repos" in keys, "repos must be in the activation_flow"
    repos = next(f for f in flow.fields if f.key == "sensors.git_activity.repos")
    assert repos.type == "path" and repos.required is True
