"""Chrome history timeline plugin."""
from __future__ import annotations

from collections import Counter
from typing import Any

from magi_plugin_sdk import (
    ExtractionProfileSpec,
    Plugin,
    SourceSpec,
    SummaryProfileSpec,
)

from .chrome_reader import _default_chrome_root
from .source import ChromeHistoryTimelineSource


DEFAULT_SETTINGS = {
    "enabled": False,
    "sync_mode": "interval",
    "sync_interval_minutes": 30,
    "default_retention_mode": "analyze_only",
    "storage_mode": "managed",
    "profile": "Default",
    "merge_window_minutes": 30,
    "max_items_per_sync": 1000,
    "initial_sync_policy": "lookback_days",
    "initial_sync_lookback_days": 7,
    "initial_sync_configured": False,
    "filter_domains": [],
    "filter_keywords": [],
}
_SESSION_GAP_SECONDS = 30 * 60
_CONTENT_INTEREST_OBJECT_TYPES = [
    "topic",
    "media",
    "person",
    "group",
    "organization",
    "product",
    "technology",
]
BROWSER_HISTORY_CAPABILITY_METADATA = {
    "capability_id": "browser_history",
    "capability_display_name": "Browser History",
    "capability_description": "Manage browser history sources that feed the timeline.",
    "entry_id": "chrome",
    "entry_display_name": "Chrome",
    "entry_description": "Local Google Chrome browsing history.",
    "entry_order": 10,
}


def _budget_int(budget: object | None, key: str, default: int) -> int:
    if budget is None:
        return int(default)
    if isinstance(budget, dict):
        raw = budget.get(key, default)
    else:
        raw = getattr(budget, key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


class ChromeHistoryPlugin(Plugin):
    """Registers the Chrome history timeline source."""

    def get_extraction_profiles(self) -> list[ExtractionProfileSpec]:
        return [
            ExtractionProfileSpec(
                profile_id="source.chrome_history",
                source_types=["chrome_history"],
                allowed_entity_types=[
                    "product",
                    "software",
                    "technology",
                    "media",
                    "person",
                    "organization",
                    "topic",
                ],
                allowed_predicates=[
                    "VISITED",
                    "USES",
                    "INTERESTED_IN",
                    "FOLLOWS",
                    "VIEWED",
                    "WORKS_WITH",
                ],
                structured_allowed_entity_types=[
                    "presence",
                    "product",
                    "software",
                    "technology",
                    "media",
                    "person",
                    "group",
                    "organization",
                    "topic",
                ],
                structured_allowed_predicates=[
                    "VISITED",
                    "USES",
                    "INTERESTED_IN",
                    "FOLLOWS",
                    "VIEWED",
                    "WORKS_WITH",
                    "ON_PLATFORM",
                    "PRESENCE_OF",
                    "LOCATED_IN",
                ],
                allowed_assertion_families=["interest_profile"],
                allow_graph=True,
                allow_assertion=True,
                allowed_assertion_traits=["interest.*"],
                derived_assertion_specs=[
                    {
                        "rule_id": "chrome_history.content_interest",
                        "source_predicates": ["INTERESTED_IN"],
                        "source_types": ["chrome_history"],
                        "trait_family": "interest_profile",
                        "trait_name_template": "interest.{object_slug}",
                        "min_observations": 3,
                        "min_distinct_days": 2,
                        "signal_preset": "passive_exposure",
                        "durable_permitted": False,
                        "object_types": _CONTENT_INTEREST_OBJECT_TYPES,
                        "source_domains": ["external_activity"],
                        "value_strategy": "canonical_name",
                    }
                ],
                extraction_instructions=(
                    "These events are browser history page titles, NOT user-authored messages.\n"
                    "Page titles often follow patterns like '{content} - {platform}' or\n"
                    "'{content} | {platform}'. Treat the platform part (YouTube, 哔哩哔哩,\n"
                    "GitHub, etc.) as a `software` entity, and the content part as the\n"
                    "actual subject (media, person, project, topic).\n\n"
                    "Predicate guidance for browsing behavior:\n"
                    "- USES: only for tool/platform usage (e.g., user uses GitHub, ChatGPT)\n"
                    "- INTERESTED_IN: when the user repeatedly browses content on a topic\n"
                    "  (e.g., AI papers, a TV show, a game)\n"
                    "- VIEWED: for individual content consumption (a specific video, article)\n"
                    "- FOLLOWS: when visiting a specific creator or person's page\n"
                    "- WORKS_WITH: for professional tools/technologies seen in work context\n\n"
                    "Assertion guidance:\n"
                    "- Do not emit Phase 2 assertion candidates for browsing events. Repeated\n"
                    "  INTERESTED_IN content evidence may be aggregated later by the host-owned\n"
                    "  derived interest rule declared in this profile.\n\n"
                    "Entity extraction rules (IMPORTANT):\n"
                    "- Preserve the source title language/script for content entities. Do NOT\n"
                    "  translate Chinese, Japanese, Korean, or other non-Latin names into\n"
                    "  English, pinyin, romaji, or URL-style slugs. If a known English title or\n"
                    "  romanization is useful, put it in alias_signals only.\n"
                    "- Do NOT infer the content entity name from URL domains or path slugs when\n"
                    "  the page title contains a readable subject name. Domains such as\n"
                    "  fandom.com, wiki.gg, wikipedia.org, google.com, and platform hostnames\n"
                    "  are source/platform context, not the canonical content name.\n"
                    "- For Fandom/Wiki-style titles such as '{page} | {work} Wiki | Fandom',\n"
                    "  extract '{page}' and '{work}' in their original language. Treat Fandom\n"
                    "  or Wiki as the platform/context, not as part of the content entity name.\n"
                    "- Be SELECTIVE: only extract entities that reveal user interests,\n"
                    "  habits, or tool usage. Not every page title deserves an entity.\n"
                    "- SKIP noise: error messages, email addresses, IP addresses,\n"
                    "  UI element names (Home, Inbox, Schema Panel), authentication pages,\n"
                    "  and generic navigation titles are NOT entities.\n"
                    "- MERGE related content: multiple pages about the same game, show,\n"
                    "  work, project, or topic should map to ONE entity with a concise\n"
                    "  canonical name, not one entity per page title. Strip page-specific\n"
                    "  qualifiers such as guides, answer pages, walkthroughs, episode titles,\n"
                    "  locations, quests, or wiki subpages when they are clearly about the\n"
                    "  same core subject. E.g., '{work} guide', '{work} answer page',\n"
                    "  '{work} location walkthrough' -> single entity '{work}'.\n"
                    "- Keep canonical names SHORT: use the core subject name, not the\n"
                    "  full page title. E.g., 'Joe Pera Talks With You' not\n"
                    "  'Joe Pera Talks With You 豆瓣'.\n"
                    "- Only use allowed entity types: software, product, technology,\n"
                    "  media, person, organization, topic. Do NOT use virtual_object,\n"
                    "  activity, concept, skill, food, health_metric, or other.\n"
                    "- Do NOT use platform names as alias_signals for content entities.\n"
                    "- Keep entity types consistent: a website/app is always `software`,\n"
                    "  not `activity` or `organization`."
                ),
            )
        ]

    def get_sources(self) -> list[tuple[str, object, SourceSpec]]:
        settings = {}
        sources_settings = self.settings.get("sources", {})
        if isinstance(sources_settings, dict):
            settings = dict(sources_settings.get("chrome_history", {}))
        source = ChromeHistoryTimelineSource(
            retention_mode=str(settings.get("default_retention_mode") or DEFAULT_SETTINGS["default_retention_mode"]),
            source_path=str(settings.get("source_path") or _default_chrome_root()),
            profile=str(settings.get("profile") or DEFAULT_SETTINGS["profile"]),
            merge_window_minutes=int(
                settings.get("merge_window_minutes", DEFAULT_SETTINGS["merge_window_minutes"])
            ),
        )
        return [
            (
                "timeline.chrome_history",
                source,
                SourceSpec(
                    source_id="timeline.chrome_history",
                    display_name="Chrome History",
                    description="Local Google Chrome browsing history ingested into the user timeline.",
                    domain="timeline",
                    surface="timeline",
                    sync_mode=str(settings.get("sync_mode", DEFAULT_SETTINGS["sync_mode"])),
                    polling_mode=getattr(source, "polling_mode", "interval"),
                    fields=[
                        field.model_copy(deep=True)
                        for field in sorted(self.manifest.settings_fields, key=lambda field: field.order)
                        if field.surface == "timeline" and field.section != "activation"
                    ],
                    metadata={
                        "source_type": "chrome_history",
                        "default_settings": {
                            field.key.rsplit(".", 1)[-1]: field.model_copy(deep=True).default
                            for field in self.manifest.settings_fields
                            if field.key.startswith("sources.") and field.type != "secret"
                        },
                        "activation_flow": self.manifest.activation_flow.model_dump() if self.manifest.activation_flow is not None else None,
                        **BROWSER_HISTORY_CAPABILITY_METADATA,
                    },
                ),
            )
        ]

    def get_summary_profiles(self) -> list[SummaryProfileSpec]:
        """Declare a daily browser_activity summary profile.

        The host runs this profile on a settle-window cadence and stores
        results under summary_category="browser_activity", which the
        activity_summary retrieval mode looks up directly.
        """
        return [
            SummaryProfileSpec(
                profile_id="chrome-history:browser_activity",
                summary_category="browser_activity",
                source_types=["chrome_history"],
                windows=["day"],
                settle_window_seconds=300,
                min_events=8,
                intent_verbs=[
                    "浏览",
                    "看了",
                    "看过",
                    "查了",
                    "搜了",
                    "搜过",
                    "browse",
                    "browsing",
                    "visited",
                    "watched",
                    "read",
                ],
                prompt_hints={"category": "browser_activity"},
            )
        ]

    def build_temporal_summary_features(
        self,
        *,
        source_type: str,
        events: list[dict[str, Any]],
        summary_category: str,
        period_start: float,
        period_end: float,
        budget: object | None = None,
    ) -> dict[str, object] | None:
        """Build browser-specific temporal summary features from Chrome history events."""

        _ = summary_category, period_start, period_end
        if source_type != "chrome_history":
            return None

        domain_counter: Counter[str] = Counter()
        visit_count = 0
        timestamps: list[float] = []
        for event in events:
            metadata = event.get("metadata_json")
            if not isinstance(metadata, dict):
                continue
            activity_snapshot = metadata.get("activity_snapshot")
            if not isinstance(activity_snapshot, dict):
                continue
            provenance = activity_snapshot.get("provenance")
            if not isinstance(provenance, dict):
                continue
            domain = str(provenance.get("domain") or "").strip().lower()
            if not domain:
                continue
            domain_counter[domain] += 1
            visit_count += max(1, int(provenance.get("merged_visit_count") or 1))
            if event.get("timestamp") is not None:
                timestamps.append(float(event["timestamp"]))

        if not domain_counter:
            return None

        top_domains = [
            {"domain": domain, "count": count}
            for domain, count in domain_counter.most_common(3)
        ]
        revisit_domains = [
            domain
            for domain, count in domain_counter.most_common()
            if count >= 2
        ]
        unique_domain_count = len(domain_counter)
        top_domain = top_domains[0]["domain"] if top_domains else None
        top_domain_count = int(top_domains[0]["count"]) if top_domains else 0
        focus_share = (top_domain_count / len(events)) if events else 0.0
        session_count = 1
        if timestamps:
            ordered_timestamps = sorted(timestamps)
            session_count = 1
            for previous, current in zip(ordered_timestamps, ordered_timestamps[1:]):
                if current - previous > _SESSION_GAP_SECONDS:
                    session_count += 1

        summary_lines: list[str] = []
        if top_domains and top_domain:
            if focus_share >= 0.6:
                summary_lines.append(f"Browsing concentrated heavily on {top_domain}.")
            else:
                joined = " and ".join(item["domain"] for item in top_domains[:2])
                summary_lines.append(f"Browsing focused on {joined}.")
        if revisit_domains:
            joined = " and ".join(revisit_domains[:2])
            summary_lines.append(f"Repeated visits clustered around {joined}.")
        if unique_domain_count <= 3:
            summary_lines.append("Browsing stayed within a small set of sites.")
        if session_count >= 2:
            summary_lines.append(f"Browsing unfolded across {session_count} distinct sessions.")
        covered_event_count = len(events)
        total_event_count = _budget_int(budget, "total_event_count", covered_event_count)
        omitted_event_count = max(0, total_event_count - covered_event_count)
        if omitted_event_count > 0:
            summary_lines.append(
                f"Browser feature coverage used {covered_event_count} representative events; {omitted_event_count} additional events were compacted."
            )

        return {
            "feature_type": "chrome_history",
            "event_count": covered_event_count,
            "total_event_count": total_event_count,
            "covered_event_count": covered_event_count,
            "omitted_event_count": omitted_event_count,
            "coverage_ratio": (covered_event_count / total_event_count) if total_event_count else None,
            "visit_count": visit_count,
            "unique_domain_count": unique_domain_count,
            "focus_domain": top_domain,
            "focus_share": focus_share,
            "session_count": session_count,
            "top_domains": top_domains,
            "revisit_domains": revisit_domains,
            "summary_lines": summary_lines,
        }
