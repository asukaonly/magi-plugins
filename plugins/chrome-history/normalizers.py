"""Normalization helpers for Chrome history timeline ingestion."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse, urlunparse

WINDOWS_TO_UNIX_EPOCH_SECONDS = 11644473600
BURST_WINDOW_SECONDS = 30 * 60.0
NOISE_PATH_TOKENS = (
    "login",
    "signin",
    "sign-in",
    "auth",
    "oauth",
    "callback",
    "redirect",
    "payment",
    "checkout",
)
NOISE_TITLE_TOKENS = (
    "sign in",
    "login",
    "redirecting",
    "callback",
    "payment",
    "checkout",
)


def chrome_time_to_unix_seconds(value: int | float | str | None) -> float:
    """Convert Chrome/WebKit microseconds since 1601 into Unix seconds."""

    if value in (None, "", 0, "0"):
        return 0.0
    numeric = float(value)
    return max(0.0, (numeric / 1_000_000.0) - WINDOWS_TO_UNIX_EPOCH_SECONDS)


_UNREAD_COUNT_PREFIX = re.compile(r"^(?:\(\d+\)\s*)+")


def normalize_title(value: str | None) -> str:
    """Return a whitespace-normalized title string.

    Strips leading unread-count prefixes such as ``(46)`` that many web apps
    (Discord, Gmail, Slack, …) prepend to the page title — those numbers churn
    on every notification and would otherwise prevent burst-merging the same
    page across visits.
    """

    collapsed = " ".join(str(value or "").split()).strip()
    if not collapsed:
        return collapsed
    return _UNREAD_COUNT_PREFIX.sub("", collapsed).strip()


def normalize_domain(url: str) -> str:
    """Return a normalized hostname for a URL."""

    parsed = urlparse(str(url or ""))
    hostname = (parsed.hostname or parsed.netloc or "").strip().lower()
    if hostname.startswith("www."):
        return hostname[4:]
    return hostname


def canonicalize_url(url: str) -> str:
    """Return a stable URL used for display and burst grouping.

    The canonical form intentionally drops fragments so client-side state churn
    does not create a new timeline item for every in-page update.
    """

    parsed = urlparse(str(url or "").strip())
    hostname = normalize_domain(url)
    if not hostname:
        return str(url or "").strip()
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query = parsed.query
    return urlunparse(("https", hostname, path, "", query, ""))


def burst_merge_key(url: str, title: str | None) -> str:
    """Return the semantic merge key used for burst grouping.

    Single-page apps such as Discord, Slack, and Notion encode their current
    state (channel, thread, message id, …) in the URL **path** rather than in
    query parameters, so two visits to "the same page" frequently differ in
    path. To keep burst-merging working across these surfaces the key uses
    only the hostname plus the normalized title, which already captures the
    user-visible page identity. Search-result and other pages whose title
    rarely changes are still distinguished by ``normalize_title``.
    """

    hostname = normalize_domain(url)
    if not hostname:
        return ""
    normalized_title = normalize_title(title)
    if not normalized_title:
        return ""
    return f"{hostname}|{normalized_title.lower()}"


def site_node_id(domain: str) -> str:
    """Build the canonical site node id for L2."""

    return f"site:{domain}"


def is_noise_visit(item: dict[str, Any]) -> bool:
    """Return whether a visit looks like a navigation-only or noise page."""

    title = str(item.get("title") or "").strip().lower()
    url = str(item.get("url") or "")
    parsed = urlparse(url)
    path = parsed.path.strip("/").lower()
    if not title:
        return True
    if any(token in title for token in NOISE_TITLE_TOKENS):
        return True
    return any(token in path for token in NOISE_PATH_TOKENS)


def should_mark_viewed(item: dict[str, Any]) -> bool:
    """Return whether a visit is strong enough to emit a VIEWED relation."""

    url = str(item.get("canonical_url") or item.get("url") or "")
    title = normalize_title(str(item.get("title") or ""))
    visit_count = max(
        int(item.get("visit_count") or 0),
        int(item.get("merged_visit_count") or 0),
    )
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if is_noise_visit(item):
        return False
    if title and path:
        return True
    return visit_count >= 3 and bool(title)


def build_relation_candidates(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate conservative relation candidates for a history item."""

    domain = str(item.get("domain") or normalize_domain(str(item.get("url") or ""))).strip().lower()
    if not domain:
        return []
    observed_at = float(item.get("visit_time") or 0.0)
    object_id = site_node_id(domain)
    object_attributes = {
        "domain": domain,
        "label": domain,
        "source_kind": "site",
    }
    if not should_mark_viewed(item):
        return []
    return [
        {
            "subject_id": "user:self",
            "subject_type": "user",
            "predicate": "VIEWED",
            "object_id": object_id,
            "object_type": "site",
            "confidence": 0.78,
            "observed_at": observed_at,
            "object_attributes": object_attributes,
        }
    ]


_TITLE_SEPARATORS = re.compile(r"\s+[\-–—|]\s+")

KNOWN_PLATFORM_DOMAINS: dict[str, str] = {
    "fandom.com": "Fandom",
    "github.com": "GitHub",
    "youtube.com": "YouTube",
    "bilibili.com": "Bilibili",
    "douyin.com": "Douyin",
    "zhihu.com": "Zhihu",
    "weibo.com": "Weibo",
    "x.com": "X",
    "twitter.com": "Twitter",
    "reddit.com": "Reddit",
    "medium.com": "Medium",
    "stackoverflow.com": "Stack Overflow",
    "wikipedia.org": "Wikipedia",
    "wiki.gg": "Wiki.gg",
    "google.com": "Google",
    "last.fm": "Last.fm",
    "spotify.com": "Spotify",
    "netflix.com": "Netflix",
    "twitch.tv": "Twitch",
    "taobao.com": "Taobao",
    "jd.com": "JD",
    "xiaohongshu.com": "Xiaohongshu",
}

_PLATFORM_SUFFIX_VARIANTS: dict[str, str] = {}
for _domain, _label in KNOWN_PLATFORM_DOMAINS.items():
    _PLATFORM_SUFFIX_VARIANTS[_label.casefold()] = _label
    _PLATFORM_SUFFIX_VARIANTS[_domain.split(".")[0].casefold()] = _label
_PLATFORM_SUFFIX_VARIANTS.update({
    "哔哩哔哩": "Bilibili",
    "b站": "Bilibili",
    "bilibili": "Bilibili",
    "抖音": "Douyin",
    "tiktok": "Douyin",
    "知乎": "Zhihu",
    "微博": "Weibo",
    "淘宝": "Taobao",
    "京东": "JD",
    "小红书": "Xiaohongshu",
    "google search": "Google",
    "google 搜索": "Google",
    "fandom": "Fandom",
    "wiki.gg": "Wiki.gg",
})

_WIKI_CONTEXT_SUFFIX = re.compile(r"\s*(?:wiki|维基|百科)\s*$", re.IGNORECASE)


def _append_entity_hint(
    hints: list[dict[str, Any]],
    *,
    mention_text: str,
    entity_type: str,
) -> None:
    """Append an entity hint while preserving order and avoiding duplicates."""

    cleaned = mention_text.strip()
    if len(cleaned) < 2:
        return
    key = (cleaned.casefold(), entity_type)
    existing = {
        (str(item.get("mention_text") or "").casefold(), str(item.get("entity_type") or ""))
        for item in hints
    }
    if key in existing:
        return
    hints.append(
        {
            "mention_text": cleaned,
            "entity_type": entity_type,
            "canonical_name_hint": cleaned,
        }
    )


def _strip_wiki_context_suffix(segment: str) -> str | None:
    """Return the readable work/site subject from a Wiki/Fandom context segment."""

    cleaned = segment.strip()
    if not cleaned:
        return None
    without_suffix = _WIKI_CONTEXT_SUFFIX.sub("", cleaned).strip()
    if without_suffix and without_suffix != cleaned:
        return without_suffix
    return None


def _match_platform_suffix(segment: str) -> str | None:
    """Return canonical platform label if segment matches a known platform."""
    cleaned = segment.strip()
    if not cleaned:
        return None
    # "哔哩哔哩_bilibili" → strip "_bilibili" variations
    for variant in ("_bilibili", " - bilibili"):
        if cleaned.casefold().endswith(variant.casefold()):
            cleaned = cleaned[: -len(variant)].strip()
            if not cleaned:
                return "Bilibili"
    return _PLATFORM_SUFFIX_VARIANTS.get(cleaned.casefold())


def parse_title_entities(
    title: str,
    domain: str,
) -> list[dict[str, Any]]:
    """Extract structured entity hints from a Chrome page title.

    Splits common ``{content} - {platform}`` patterns and returns entity hints
    for the recognised platform and any meaningful content label.
    """
    hints: list[dict[str, Any]] = []
    normalized = normalize_title(title)
    if not normalized:
        return hints

    # Try matching a known platform from the domain first
    domain_platform: str | None = None
    for known_domain, label in KNOWN_PLATFORM_DOMAINS.items():
        if domain.endswith(known_domain):
            domain_platform = label
            break

    # Split the title by common separators and check the last segment
    segments = [
        segment.strip()
        for segment in _TITLE_SEPARATORS.split(normalized)
        if segment.strip()
    ]
    detected_platform: str | None = None
    content_part: str = normalized
    wiki_context: str | None = None

    if len(segments) >= 2:
        last_segment = segments[-1].strip()
        platform_match = _match_platform_suffix(last_segment)
        if platform_match:
            detected_platform = platform_match
            content_segments = segments[:-1]
            content_part = content_segments[0].strip()
            if len(content_segments) >= 2:
                wiki_context = _strip_wiki_context_suffix(content_segments[-1])
            if not content_part:
                content_part = normalized
        elif domain_platform and len(segments) >= 2:
            content_part = segments[0].strip()
            wiki_context = _strip_wiki_context_suffix(segments[-1])

    platform = detected_platform or domain_platform
    if platform:
        _append_entity_hint(hints, mention_text=platform, entity_type="software")

    # Content entity: only if title had a separator and content part is meaningful
    if platform and content_part and content_part != normalized:
        content_type = "topic" if platform in {"Fandom", "Wiki.gg"} else "media"
        _append_entity_hint(hints, mention_text=content_part, entity_type=content_type)

    if wiki_context and wiki_context != content_part:
        _append_entity_hint(hints, mention_text=wiki_context, entity_type="media")

    return hints


def should_merge_visit(
    current: dict[str, Any],
    candidate: dict[str, Any],
    *,
    burst_window_seconds: float = BURST_WINDOW_SECONDS,
) -> bool:
    """Return whether two visits should collapse into one timeline item."""

    current_key = str(
        current.get("burst_merge_key")
        or burst_merge_key(str(current.get("url") or ""), current.get("title"))
    )
    candidate_key = str(
        candidate.get("burst_merge_key")
        or burst_merge_key(str(candidate.get("url") or ""), candidate.get("title"))
    )
    if not current_key or current_key != candidate_key:
        return False
    current_domain = str(current.get("domain") or "")
    candidate_domain = str(candidate.get("domain") or "")
    if current_domain != candidate_domain:
        return False
    current_time = float(current.get("visit_time") or 0.0)
    candidate_time = float(candidate.get("visit_time") or 0.0)
    if candidate_time - current_time > burst_window_seconds:
        return False
    current_title = normalize_title(current.get("title"))
    candidate_title = normalize_title(candidate.get("title"))
    if current_title and candidate_title and current_title != candidate_title:
        return False
    return True
