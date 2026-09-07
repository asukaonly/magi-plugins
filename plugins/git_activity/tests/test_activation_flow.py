"""Git activity activation requires reviewed repository paths."""
from pathlib import Path

from sdk_test_support import bind_test_plugin, load_plugin


def test_git_activity_flow_includes_repos() -> None:
    plugin = bind_test_plugin(load_plugin(Path(__file__).resolve().parents[1] / "plugin.toml"))
    flow = plugin.manifest.activation_flow
    assert flow is not None
    repos = next(field for field in flow.fields if field.key == "sources.git_activity.repos")
    assert repos.type == "path" and repos.required is True
    assert flow.first_context is not None
    assert flow.first_context.max_items_per_sync == 200
    assert plugin.get_sources()[0][2].metadata["activation_flow"] == flow.model_dump()
