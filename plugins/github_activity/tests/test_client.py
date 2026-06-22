"""GitHub Activity client behavior."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


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


def test_repository_slug_normalization_accepts_urls_and_owner_repo() -> None:
    client = _load_module("client")

    assert client.normalize_repository_slug("https://github.com/openai/codex") == "openai/codex"
    assert client.normalize_repository_slug("git@github.com:openai/codex.git") == "openai/codex"
    assert client.normalize_repository_slug(" openai/codex ") == "openai/codex"


def test_collect_repository_events_maps_pull_issue_commit_and_check_payloads() -> None:
    client = _load_module("client")
    requests: list[str] = []

    def fake_request(path: str, params=None):
        requests.append(path)
        if path == "/repos/acme/app/pulls":
            return [
                {
                    "number": 7,
                    "title": "Add GitHub sync",
                    "state": "open",
                    "html_url": "https://github.com/acme/app/pull/7",
                    "updated_at": "2026-06-18T01:02:03Z",
                    "user": {"login": "asuka"},
                    "head": {"sha": "abc1234"},
                }
            ]
        if path == "/repos/acme/app/pulls/7/reviews":
            return [{"id": 99, "state": "APPROVED", "user": {"login": "teammate"}, "submitted_at": "2026-06-18T02:00:00Z"}]
        if path == "/repos/acme/app/issues":
            return [
                {"number": 8, "title": "Bug report", "state": "open", "html_url": "https://github.com/acme/app/issues/8", "updated_at": "2026-06-18T03:00:00Z", "user": {"login": "asuka"}},
                {"number": 7, "title": "PR issue mirror", "pull_request": {}},
            ]
        if path == "/repos/acme/app/commits":
            return [{"sha": "abc1234", "html_url": "https://github.com/acme/app/commit/abc1234", "commit": {"message": "Wire sync", "author": {"date": "2026-06-18T04:00:00Z"}}, "author": {"login": "asuka"}}]
        if path == "/repos/acme/app/commits/abc1234/check-runs":
            return {"check_runs": [{"id": 5, "name": "tests", "status": "completed", "conclusion": "success", "html_url": "https://github.com/acme/app/runs/5", "completed_at": "2026-06-18T04:05:00Z"}]}
        raise AssertionError(path)

    gh = client.GitHubActivityClient(access_token="token", request_func=fake_request)
    events = gh.collect_repository_events("acme/app", since_iso="2026-06-17T00:00:00Z", limit=20)

    assert [event["event_kind"] for event in events] == ["pull_request", "pull_request_review", "issue", "commit", "check_run"]
    assert events[0]["repository"] == "acme/app"
    assert events[0]["source_item_id"] == "github:acme/app:pull_request:7"
    assert events[1]["actor"] == "teammate"
    assert events[2]["source_item_id"] == "github:acme/app:issue:8"
    assert events[3]["summary"] == "Wire sync"
    assert events[4]["state"] == "success"
    assert "/repos/acme/app/pulls/7/reviews" in requests


def test_device_auth_start_and_poll_use_github_device_flow_contract() -> None:
    client = _load_module("client")
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_form(url: str, data: dict[str, str]):
        calls.append((url, data))
        if url.endswith("/login/device/code"):
            return {
                "device_code": "device",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": 5,
            }
        return {
            "access_token": "ghu_token",
            "refresh_token": "ghr_refresh",
            "expires_in": 28800,
            "refresh_token_expires_in": 15897600,
            "token_type": "bearer",
        }

    auth = client.GitHubDeviceAuthClient(client_id="client-id", form_request_func=fake_form)
    start = auth.start()
    token = auth.poll("device")

    assert start.user_code == "ABCD-EFGH"
    assert token.access_token == "ghu_token"
    assert calls[0][1]["client_id"] == "client-id"
    assert calls[1][1]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
