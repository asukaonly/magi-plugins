"""Timeline source for local Chrome history."""

from __future__ import annotations

from magi_plugin_sdk.runtime import SourceChange, SourceChangeBatch

import logging
import re
import time
from datetime import date, datetime, time as datetime_time, timedelta
from typing import Any

from magi_plugin_sdk import UserContentClearContext
from magi_plugin_sdk.sources import (
    ContentBlock,
    L2BatchPolicy,
    Source,
    SourceMemoryPolicy,
    SourceOutput,
    SourceOutputMetadata,
    SourceSyncContext,
)

from .chrome_reader import ChromeHistoryReader, _default_chrome_root
from .normalizers import (
    build_fact_hints,
    build_relation_candidates,
    build_source_facets,
    normalize_domain,
    parse_title_entities,
)

logger = logging.getLogger(__name__)


class ChromeHistoryTimelineSource(Source):
    """Pull-sync source backed by the local Chrome history SQLite database."""

    source_id = "timeline.chrome_history"
    display_name = "Chrome History"
    source_type = "chrome_history"
    polling_mode = "interval"
    default_interval = 30
    update_key_fields = ("visit_id",)
    relation_edge_whitelist = ("VIEWED",)
    supports_pull_sync = True

    # P2 frequency gate — see browser_history_core; interest needs repeat visits.
    memory_policy = SourceMemoryPolicy(promotion_threshold=3)

    def __init__(
        self,
        *,
        retention_mode: str | None = None,
        source_path: str | None = None,
        profile: str = "Default",
        merge_window_minutes: int = 30,
        reader: ChromeHistoryReader | None = None,
    ) -> None:
        super().__init__()
        self.retention_mode = retention_mode or "analyze_only"
        self.source_path = source_path
        self.profile = profile
        self.merge_window_minutes = merge_window_minutes
        self._reader = reader or ChromeHistoryReader()

    def source_item_identity(self, item: dict[str, Any]) -> str:
        return str(item.get("source_item_id") or item.get("visit_id") or "")


    def l2_batch_policy(self, output: SourceOutput) -> L2BatchPolicy | None:
        profile = str(output.provenance.get("profile") or self.profile or "").strip()
        ts = output.occurred_at or output.captured_at or time.time()
        try:
            day = time.strftime("%Y%m%d", time.localtime(ts))
        except (OSError, OverflowError, ValueError):
            day = "unknown"
        owner = f"{self.source_type}:{profile or 'default'}:{day}"
        return L2BatchPolicy(
            owner=owner,
            catch_up_owner=f"{self.source_type}:{profile or 'default'}:catchup",
            max_events=20,
            min_ready_events=8,
            max_wait_seconds=300,
        )

    async def collect_items(self, context: SourceSyncContext) -> SourceChangeBatch:
        prepare_temp_storage = getattr(self._reader, "prepare_temp_storage", None)
        if callable(prepare_temp_storage):
            temp_root = (
                context.runtime_paths.plugin_cache_dir(self.plugin_id)
                / "temporary-database-copies"
                / "browser-history"
            )
            prepare_temp_storage(temp_root)
        source_settings = (
            context.plugin_settings.get("sources", {}).get(self.source_type, {})
            if isinstance(context.plugin_settings.get("sources", {}), dict)
            else {}
        )
        source_path = str(
            source_settings.get("source_path") or self.source_path or _default_chrome_root()
        )
        profile = str(source_settings.get("profile") or self.profile or "Default")
        merge_window_minutes = max(
            1,
            int(source_settings.get("merge_window_minutes", self.merge_window_minutes)),
        )
        initial_sync_policy = str(source_settings.get("initial_sync_policy") or "lookback_days")
        initial_sync_lookback_days = max(
            1, int(source_settings.get("initial_sync_lookback_days", 7))
        )
        initial_lookback_hours: int | None = max(1, initial_sync_lookback_days) * 24
        initial_start_time: float | None = None
        initial_end_time: float | None = None
        if initial_sync_policy == "custom_range":
            initial_start_time, initial_end_time = _custom_range_bounds(source_settings)
            initial_lookback_hours = None
        if context.last_cursor is None:
            if initial_sync_policy == "full":
                initial_lookback_hours = None
            elif initial_sync_policy == "from_now":
                latest_visit_id = self._reader.get_latest_visit_id(
                    source_path=source_path, profile=profile
                )
                return SourceChangeBatch(
                    changes=[],
                    next_cursor=str(latest_visit_id) if latest_visit_id > 0 else None,
                    watermark_ts=context.last_success_at or time.time(),
                    stats={
                        "count": 0,
                        "profile": profile,
                        "raw_count": 0,
                        "initial_sync_policy": initial_sync_policy,
                    },
                )
        raw_items = self._reader.read_visits(
            source_path=source_path,
            profile=profile,
            limit=max(1, context.limit),
            last_cursor=context.last_cursor,
            initial_lookback_hours=initial_lookback_hours,
            initial_start_time=initial_start_time,
            initial_end_time=initial_end_time,
            merge_window_seconds=float(merge_window_minutes) * 60.0,
        )
        next_cursor = context.last_cursor
        watermark_ts = context.last_success_at
        if raw_items:
            raw_max_visit_id = max(
                int(item.get("last_visit_id") or item.get("visit_id") or 0) for item in raw_items
            )
            next_cursor = str(raw_max_visit_id) if raw_max_visit_id > 0 else context.last_cursor
            watermark_ts = max(float(item.get("visit_time") or 0.0) for item in raw_items)
        continued_count = sum(1 for item in raw_items if not item.get("_emit_item", True))
        items = [item for item in raw_items if item.get("_emit_item", True)]
        filter_domains_raw = source_settings.get("filter_domains") or []
        filter_keywords_raw = source_settings.get("filter_keywords") or []
        domain_patterns = _compile_domain_patterns(filter_domains_raw)
        keyword_terms = _normalize_keywords(filter_keywords_raw)
        filtered_count = 0
        if domain_patterns or keyword_terms:
            kept: list[dict[str, Any]] = []
            for item in items:
                if _item_matches_filters(item, domain_patterns, keyword_terms):
                    filtered_count += 1
                    continue
                kept.append(item)
            items = kept
        return SourceChangeBatch(
            changes=[SourceChange(object_id=self.source_item_identity(item), version=self.source_item_version_fingerprint(item), payload=item) for item in items],
            next_cursor=str(next_cursor) if next_cursor else None,
            watermark_ts=watermark_ts,
            complete=not (sum(
                    int(item.get("merged_visit_count") or 1)
                    for item in raw_items
                    if item.get("_has_new_visit", True)
                )
                >= max(1, context.limit)),
            stats={
                "count": len(items),
                "profile": profile,
                "raw_count": sum(int(item.get("merged_visit_count") or 1) for item in items),
                "initial_sync_policy": (
                    initial_sync_policy if context.last_cursor is None else "incremental"
                ),
                "filtered_count": filtered_count,
                "continued_count": continued_count,
                "merge_window_minutes": merge_window_minutes,
                "has_more": sum(
                    int(item.get("merged_visit_count") or 1)
                    for item in raw_items
                    if item.get("_has_new_visit", True)
                )
                >= max(1, context.limit),
            },
        )

    async def clear_user_content(self, context: UserContentClearContext) -> None:
        """Remove crash-residual copies without touching the Chrome profile."""

        clear_temp_copies = getattr(self._reader, "clear_temp_copies", None)
        if callable(clear_temp_copies):
            temp_root = (
                context.runtime_paths.plugin_cache_dir(context.plugin_id)
                / "temporary-database-copies"
                / "browser-history"
            )
            clear_temp_copies(temp_root)

    async def build_output(self, item: dict[str, Any]) -> SourceOutput:
        url = str(item.get("canonical_url") or item.get("url") or "")
        title = str(item.get("title") or item.get("domain") or url or "Visited page")
        domain = str(item.get("domain") or normalize_domain(url))
        merged_visit_count = max(1, int(item.get("merged_visit_count") or 1))
        # Use i18n for summary
        if merged_visit_count == 1:
            summary = self.t("summary.single_visit", title=title)
        else:
            summary = self.t("summary.multiple_visits", title=title, count=merged_visit_count)
        content_blocks = [
            ContentBlock(kind="text", value=url),
        ]
        if title:
            content_blocks.append(ContentBlock(kind="text", value=title))
        return self._build_output(
            source_item_id=self.source_item_identity(item),
            activity=self._build_activity(
                source=self._build_activity_facet(
                    code="chrome",
                    i18n_key="activity.source.chrome",
                    fallback="Chrome",
                    embedding_fallback="Chrome",
                ),
                action=self._build_activity_facet(
                    code="browse",
                    i18n_key="activity.action.browse",
                    fallback="Browsing",
                    embedding_fallback="浏览",
                ),
                object=self._build_activity_facet(
                    code="web_page",
                    i18n_key="activity.object.page",
                    fallback="Page",
                    embedding_fallback="网页",
                ),
            ),
            narration=self._build_narration(title=title, body=summary),
            occurred_at=float(item.get("visit_time") or 0.0),
            content_blocks=content_blocks,
            tags=[tag for tag in ("chrome_history", domain) if tag],
            provenance={
                "source_id": self.source_id,
                "browser": "chrome",
                "profile": str(item.get("profile") or self.profile),
                "visit_id": str(item.get("visit_id") or ""),
                "first_visit_id": str(item.get("first_visit_id") or item.get("visit_id") or ""),
                "last_visit_id": str(item.get("last_visit_id") or item.get("visit_id") or ""),
                "merged_visit_count": merged_visit_count,
                "burst_start_time": float(
                    item.get("burst_start_time") or item.get("visit_time") or 0.0
                ),
                "burst_end_time": float(
                    item.get("burst_end_time") or item.get("visit_time") or 0.0
                ),
                "domain": domain,
                "from_visit": str(item.get("from_visit") or ""),
                "transition": str(item.get("transition") or ""),
                "canonical_url": url,
            },
            domain_payload={
                "retention_mode": self.retention_mode,
                "promotion_key": domain,
                "source_facets": build_source_facets(item),
            },
        )

    async def extract_metadata(self, item: dict[str, Any]) -> SourceOutputMetadata:
        domain = str(item.get("domain") or normalize_domain(str(item.get("url") or "")))
        title = str(item.get("title") or "")
        entity_hints = parse_title_entities(title, domain)
        return SourceOutputMetadata(
            entities=entity_hints,
            tags=[tag for tag in ("chrome_history", domain) if tag],
            fact_hints=build_fact_hints(item),
            relation_candidates=build_relation_candidates(item),
        )


def _compile_domain_patterns(values: Any) -> list[re.Pattern[str]]:
    """Compile user-supplied regular expressions for domain filtering."""

    if not isinstance(values, (list, tuple)):
        return []
    patterns: list[re.Pattern[str]] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            patterns.append(re.compile(text, re.IGNORECASE))
        except re.error as exc:
            logger.warning("Ignoring invalid chrome_history domain filter %r: %s", text, exc)
    return patterns


def _normalize_keywords(values: Any) -> list[str]:
    """Normalize user-supplied keyword filters to lowercase, non-empty strings."""

    if not isinstance(values, (list, tuple)):
        return []
    keywords: list[str] = []
    for raw in values:
        text = str(raw or "").strip().lower()
        if text:
            keywords.append(text)
    return keywords


def _item_matches_filters(
    item: dict[str, Any],
    domain_patterns: list[re.Pattern[str]],
    keyword_terms: list[str],
) -> bool:
    """Return True when the visit should be dropped before AI analysis."""

    domain = str(item.get("domain") or "").lower()
    if domain_patterns and domain:
        for pattern in domain_patterns:
            if pattern.search(domain):
                return True
    if keyword_terms:
        haystack_parts = [
            str(item.get("title") or ""),
            str(item.get("url") or ""),
            str(item.get("canonical_url") or ""),
        ]
        haystack = " ".join(haystack_parts).lower()
        for keyword in keyword_terms:
            if keyword in haystack:
                return True
    return False


def _custom_range_bounds(source_settings: dict[str, Any]) -> tuple[float, float]:
    """Return local-day Unix bounds for a custom history backfill."""

    start_text = str(source_settings.get("initial_sync_start_date") or "").strip()
    end_text = str(source_settings.get("initial_sync_end_date") or "").strip()
    try:
        start_date = date.fromisoformat(start_text)
        end_date = date.fromisoformat(end_text)
    except ValueError as exc:
        raise ValueError("Custom Chrome history sync requires valid start and end dates") from exc
    if start_date > end_date:
        raise ValueError("Custom Chrome history sync end date cannot be earlier than start date")
    start_at = datetime.combine(start_date, datetime_time.min)
    end_at = datetime.combine(end_date + timedelta(days=1), datetime_time.min)
    return start_at.timestamp(), end_at.timestamp()
