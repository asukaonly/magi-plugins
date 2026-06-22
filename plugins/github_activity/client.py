"""Small GitHub REST and device-flow client for local pull sync."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


GITHUB_API_BASE = "https://api.github.com"
GITHUB_WEB_BASE = "https://github.com"
GITHUB_API_VERSION = "2022-11-28"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"


RequestFunc = Callable[[str, dict[str, Any] | None], Any]
FormRequestFunc = Callable[[str, dict[str, str]], dict[str, Any]]


class GitHubClientError(RuntimeError):
    """Raised when GitHub returns an unexpected error."""


class GitHubDeviceAuthorizationPending(GitHubClientError):
    """Raised while the user has not completed device authorization."""


@dataclass(frozen=True, slots=True)
class GitHubDeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True, slots=True)
class GitHubTokenResult:
    access_token: str
    token_type: str = "bearer"
    expires_in: int | None = None
    refresh_token: str | None = None
    refresh_token_expires_in: int | None = None


def normalize_repository_slug(value: str) -> str:
    """Normalize user-entered repo references into owner/repo."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("git@github.com:"):
        text = text.removeprefix("git@github.com:")
    if text.startswith("https://github.com/"):
        text = text.removeprefix("https://github.com/")
    if text.startswith("http://github.com/"):
        text = text.removeprefix("http://github.com/")
    text = text.removesuffix(".git").strip("/")
    parts = [part for part in text.split("/") if part]
    if len(parts) < 2:
        return ""
    return f"{parts[0]}/{parts[1]}"


def iso_to_timestamp(value: str | None) -> float:
    if not value:
        return datetime.now(timezone.utc).timestamp()
    normalized = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return datetime.now(timezone.utc).timestamp()


def timestamp_to_iso(timestamp: float | int | None) -> str:
    if not timestamp:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _first_line(value: str) -> str:
    return str(value or "").strip().splitlines()[0] if str(value or "").strip() else ""


def _actor_login(value: dict[str, Any] | None, fallback: str = "") -> str:
    if isinstance(value, dict):
        login = str(value.get("login") or "").strip()
        if login:
            return login
    return fallback


def _default_json_request(access_token: str, path: str, params: dict[str, Any] | None = None) -> Any:
    query = urllib.parse.urlencode({key: value for key, value in dict(params or {}).items() if value is not None})
    url = f"{GITHUB_API_BASE}{path}"
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {access_token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "Magi-GitHub-Activity",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubClientError(f"GitHub API request failed: {exc.code} {detail}") from exc
    return json.loads(body or "null")


def _default_form_request(url: str, data: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Magi-GitHub-Activity",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubClientError(f"GitHub OAuth request failed: {exc.code} {detail}") from exc
    return json.loads(raw or "{}")


class GitHubDeviceAuthClient:
    """OAuth device-flow helper for desktop/local-only authorization."""

    def __init__(
        self,
        *,
        client_id: str,
        form_request_func: FormRequestFunc | None = None,
    ) -> None:
        self.client_id = str(client_id or "").strip()
        self._form_request = form_request_func or _default_form_request

    def start(self) -> GitHubDeviceCode:
        if not self.client_id:
            raise GitHubClientError("GitHub client_id is required.")
        data = self._form_request(
            f"{GITHUB_WEB_BASE}/login/device/code",
            {"client_id": self.client_id},
        )
        return GitHubDeviceCode(
            device_code=str(data.get("device_code") or ""),
            user_code=str(data.get("user_code") or ""),
            verification_uri=str(data.get("verification_uri") or f"{GITHUB_WEB_BASE}/login/device"),
            expires_in=int(data.get("expires_in") or 900),
            interval=int(data.get("interval") or 5),
        )

    def poll(self, device_code: str) -> GitHubTokenResult:
        data = self._form_request(
            f"{GITHUB_WEB_BASE}/login/oauth/access_token",
            {
                "client_id": self.client_id,
                "device_code": str(device_code or ""),
                "grant_type": DEVICE_GRANT_TYPE,
            },
        )
        error = str(data.get("error") or "").strip()
        if error:
            if error in {"authorization_pending", "slow_down"}:
                raise GitHubDeviceAuthorizationPending(error)
            raise GitHubClientError(error)
        token = str(data.get("access_token") or "").strip()
        if not token:
            raise GitHubClientError("GitHub did not return an access token.")
        return GitHubTokenResult(
            access_token=token,
            token_type=str(data.get("token_type") or "bearer"),
            expires_in=int(data["expires_in"]) if data.get("expires_in") is not None else None,
            refresh_token=str(data.get("refresh_token") or "") or None,
            refresh_token_expires_in=(
                int(data["refresh_token_expires_in"])
                if data.get("refresh_token_expires_in") is not None
                else None
            ),
        )


class GitHubActivityClient:
    """Collect compact GitHub activity for selected repositories."""

    def __init__(self, *, access_token: str, request_func: RequestFunc | None = None) -> None:
        self.access_token = str(access_token or "").strip()
        self._request_func = request_func

    def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self._request_func is not None:
            return self._request_func(path, params)
        if not self.access_token:
            raise GitHubClientError("GitHub access token is required.")
        return _default_json_request(self.access_token, path, params)

    def _safe_request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return self._request(path, params)
        except GitHubClientError:
            return [] if not path.endswith("/check-runs") else {"check_runs": []}

    def collect_repository_events(
        self,
        repository: str,
        *,
        since_iso: str | None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        slug = normalize_repository_slug(repository)
        if not slug:
            return []
        per_page = max(1, min(100, int(limit or 50)))
        pulls = self._safe_request(
            f"/repos/{slug}/pulls",
            {"state": "all", "sort": "updated", "direction": "desc", "per_page": per_page},
        )
        issues = self._safe_request(
            f"/repos/{slug}/issues",
            {"state": "all", "sort": "updated", "direction": "desc", "since": since_iso, "per_page": per_page},
        )
        commits = self._safe_request(
            f"/repos/{slug}/commits",
            {"since": since_iso, "per_page": per_page},
        )

        events: list[dict[str, Any]] = []
        for pull in pulls if isinstance(pulls, list) else []:
            events.append(_pull_event(slug, pull))
            number = pull.get("number")
            if number is not None:
                reviews = self._safe_request(f"/repos/{slug}/pulls/{number}/reviews", {"per_page": 20})
                for review in reviews if isinstance(reviews, list) else []:
                    events.append(_review_event(slug, number, review, pull))

        for issue in issues if isinstance(issues, list) else []:
            if isinstance(issue, dict) and "pull_request" in issue:
                continue
            events.append(_issue_event(slug, issue))

        for commit in commits if isinstance(commits, list) else []:
            events.append(_commit_event(slug, commit))
            sha = str(commit.get("sha") or "").strip()
            if sha:
                check_payload = self._safe_request(f"/repos/{slug}/commits/{sha}/check-runs", {"per_page": 20})
                check_runs = check_payload.get("check_runs") if isinstance(check_payload, dict) else []
                for check in check_runs if isinstance(check_runs, list) else []:
                    events.append(_check_run_event(slug, sha, check))

        events = [event for event in events if event.get("source_item_id")]
        return events[:per_page]


def _pull_event(repository: str, pull: dict[str, Any]) -> dict[str, Any]:
    number = pull.get("number")
    title = str(pull.get("title") or f"Pull request #{number}").strip()
    actor = _actor_login(pull.get("user") if isinstance(pull.get("user"), dict) else None)
    state = str(pull.get("merged_at") and "merged" or pull.get("state") or "").strip()
    occurred_at = str(pull.get("updated_at") or pull.get("created_at") or "")
    return {
        "source_item_id": f"github:{repository}:pull_request:{number}",
        "repository": repository,
        "event_kind": "pull_request",
        "title": title,
        "summary": f"PR #{number} {state or 'updated'} by {actor or 'unknown'}: {title}",
        "state": state,
        "actor": actor,
        "occurred_at": occurred_at,
        "url": str(pull.get("html_url") or ""),
        "number": int(number or 0),
        "sha": str((pull.get("head") or {}).get("sha") or ""),
    }


def _review_event(repository: str, number: Any, review: dict[str, Any], pull: dict[str, Any]) -> dict[str, Any]:
    review_id = review.get("id")
    actor = _actor_login(review.get("user") if isinstance(review.get("user"), dict) else None)
    state = str(review.get("state") or "").lower()
    title = str(pull.get("title") or f"Pull request #{number}").strip()
    occurred_at = str(review.get("submitted_at") or pull.get("updated_at") or "")
    return {
        "source_item_id": f"github:{repository}:pull_request_review:{review_id}",
        "repository": repository,
        "event_kind": "pull_request_review",
        "title": title,
        "summary": f"{actor or 'Someone'} reviewed PR #{number}: {state or 'reviewed'}",
        "state": state,
        "actor": actor,
        "occurred_at": occurred_at,
        "url": str(review.get("html_url") or pull.get("html_url") or ""),
        "number": int(number or 0),
    }


def _issue_event(repository: str, issue: dict[str, Any]) -> dict[str, Any]:
    number = issue.get("number")
    title = str(issue.get("title") or f"Issue #{number}").strip()
    actor = _actor_login(issue.get("user") if isinstance(issue.get("user"), dict) else None)
    state = str(issue.get("state") or "").strip()
    occurred_at = str(issue.get("updated_at") or issue.get("created_at") or "")
    return {
        "source_item_id": f"github:{repository}:issue:{number}",
        "repository": repository,
        "event_kind": "issue",
        "title": title,
        "summary": f"Issue #{number} {state or 'updated'} by {actor or 'unknown'}: {title}",
        "state": state,
        "actor": actor,
        "occurred_at": occurred_at,
        "url": str(issue.get("html_url") or ""),
        "number": int(number or 0),
    }


def _commit_event(repository: str, commit: dict[str, Any]) -> dict[str, Any]:
    sha = str(commit.get("sha") or "").strip()
    inner = commit.get("commit") if isinstance(commit.get("commit"), dict) else {}
    author = inner.get("author") if isinstance(inner.get("author"), dict) else {}
    message = _first_line(str(inner.get("message") or "Commit"))
    actor = _actor_login(commit.get("author") if isinstance(commit.get("author"), dict) else None, str(author.get("name") or ""))
    occurred_at = str(author.get("date") or commit.get("updated_at") or "")
    return {
        "source_item_id": f"github:{repository}:commit:{sha[:12]}",
        "repository": repository,
        "event_kind": "commit",
        "title": message,
        "summary": message,
        "state": "committed",
        "actor": actor,
        "occurred_at": occurred_at,
        "url": str(commit.get("html_url") or ""),
        "sha": sha,
    }


def _check_run_event(repository: str, sha: str, check: dict[str, Any]) -> dict[str, Any]:
    check_id = check.get("id")
    name = str(check.get("name") or "check").strip()
    conclusion = str(check.get("conclusion") or check.get("status") or "").strip()
    occurred_at = str(check.get("completed_at") or check.get("started_at") or "")
    return {
        "source_item_id": f"github:{repository}:check_run:{check_id}",
        "repository": repository,
        "event_kind": "check_run",
        "title": name,
        "summary": f"Check {name}: {conclusion or 'updated'}",
        "state": conclusion,
        "actor": "",
        "occurred_at": occurred_at,
        "url": str(check.get("html_url") or ""),
        "sha": sha,
    }
