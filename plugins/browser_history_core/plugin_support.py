"""Shared plugin-level helpers for browser history sensors."""
from __future__ import annotations

from collections import Counter
from typing import Any

from magi_plugin_sdk import (
    ActivationFlowSpec,
    ExtensionFieldOption,
    ExtensionFieldSpec,
    ExtractionProfileSpec,
    SummaryProfileSpec,
)

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

_EXTRACTION_INSTRUCTIONS = (
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
    "  VIEWED graph evidence may be aggregated later by the host-owned derived\n"
    "  interest rule declared in this profile.\n\n"
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
)


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


def build_activation_flow(prefix: str, browser_label: str) -> ActivationFlowSpec:
    return ActivationFlowSpec(
        title=f"Enable {browser_label} History",
        description=(
            f"{browser_label} history is sensitive local data. Choose how the first sync should seed the timeline "
            "before this source starts running."
        ),
        confirm_label="Enable source",
        cancel_label="Not now",
        enabled_key=f"{prefix}.enabled",
        configured_key=f"{prefix}.initial_sync_configured",
        fields=[
            ExtensionFieldSpec(
                key=f"{prefix}.initial_sync_policy",
                type="select",
                label="First Sync Scope",
                description="Decide how much history should be imported when this source is enabled for the first time.",
                default="lookback_days",
                options=[
                    ExtensionFieldOption(label="Sync full history", value="full"),
                    ExtensionFieldOption(label="Sync recent days", value="lookback_days"),
                    ExtensionFieldOption(label="Only new records from now on", value="from_now"),
                ],
                section="activation",
                surface="timeline",
                order=10,
            ),
            ExtensionFieldSpec(
                key=f"{prefix}.initial_sync_lookback_days",
                type="number",
                label="Recent Days",
                description="Used when the first-sync scope is set to recent days.",
                default=7,
                section="activation",
                surface="timeline",
                order=20,
                depends_on_key=f"{prefix}.initial_sync_policy",
                depends_on_values=["lookback_days"],
            ),
        ],
    )


def build_fields(
    prefix: str,
    browser_label: str,
    *,
    profile_default: str = "Default",
    profile_description: str | None = None,
) -> list[ExtensionFieldSpec]:
    return [
        ExtensionFieldSpec(
            key=f"{prefix}.enabled",
            type="switch",
            label="Enabled",
            description=f"Whether {browser_label} history sync is active.",
            default=False,
            section="general",
            surface="timeline",
            order=10,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.profile",
            type="input",
            label="Profile",
            description=profile_description
            or f"{browser_label} profile directory to read, such as Default or Profile 1.",
            default=profile_default,
            section="general",
            surface="timeline",
            order=20,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sync_mode",
            type="select",
            label="Sync Mode",
            description=f"How {browser_label} history should be synchronized.",
            default="interval",
            required=True,
            options=[
                ExtensionFieldOption(label="Manual", value="manual"),
                ExtensionFieldOption(label="Interval", value="interval"),
            ],
            section="general",
            surface="timeline",
            order=30,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sync_interval_minutes",
            type="number",
            label="Sync Interval (minutes)",
            description="Polling interval used for interval-based sync.",
            default=30,
            section="general",
            surface="timeline",
            order=40,
            depends_on_key=f"{prefix}.sync_mode",
            depends_on_values=["interval"],
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.merge_window_minutes",
            type="number",
            label="Merge Window (minutes)",
            description=(
                "Raw visits to the same page within this window are merged into one timeline item, "
                "even when other pages appear between them."
            ),
            default=30,
            section="general",
            surface="timeline",
            order=50,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.max_items_per_sync",
            type="number",
            label="Max Items Per Sync",
            description="Maximum number of history records to ingest per run.",
            default=1000,
            section="sync",
            surface="timeline",
            order=60,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.filter_domains",
            type="tags",
            label="Filter Domains (Regex)",
            description=(
                "Visits whose normalized domain matches any regex via partial search are skipped before AI analysis. "
                "Use ^...$ for exact matches."
            ),
            default=[],
            section="filters",
            surface="timeline",
            order=70,
            placeholder="e.g. ^mail\\.google\\.com$",
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.filter_keywords",
            type="tags",
            label="Filter Keywords",
            description=(
                "Visits whose URL or title contains any of these keywords are skipped before AI analysis. "
                "Case-insensitive substring match, not regex."
            ),
            default=[],
            section="filters",
            surface="timeline",
            order=80,
            placeholder="e.g. password reset",
        ),
    ]


def build_extraction_profiles(source_type: str) -> list[ExtractionProfileSpec]:
    return [
        ExtractionProfileSpec(
            profile_id=f"source.{source_type}",
            source_types=[source_type],
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
            allowed_assertion_families=["preference_profile"],
            allow_graph=True,
            allow_assertion=True,
            assertion_mode="derived",
            allowed_assertion_traits=["interest.*"],
            derived_assertion_specs=[
                {
                    "rule_id": f"{source_type}.viewed_interest",
                    "source_predicates": ["VIEWED"],
                    "source_types": [source_type],
                    "trait_family": "preference_profile",
                    "trait_name_template": "interest.{object_slug}",
                    "min_observations": 3,
                    "min_distinct_days": 1,
                    "source_domains": ["external_activity"],
                    "value_strategy": "canonical_name",
                }
            ],
            extraction_instructions=_EXTRACTION_INSTRUCTIONS,
        )
    ]


def build_summary_profile(*, source_type: str, plugin_id: str) -> SummaryProfileSpec:
    return SummaryProfileSpec(
        profile_id=f"{plugin_id}:browser_activity",
        summary_category="browser_activity",
        source_types=[source_type],
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


def build_temporal_summary_features(
    *,
    source_type: str,
    feature_type: str,
    events: list[dict[str, Any]],
    budget: object | None = None,
) -> dict[str, object] | None:
    domain_counter: Counter[str] = Counter()
    visit_count = 0
    timestamps: list[float] = []
    for event in events:
        metadata = event.get("metadata_json")
        if not isinstance(metadata, dict):
            continue
        timeline = metadata.get("timeline")
        if not isinstance(timeline, dict):
            continue
        provenance = timeline.get("provenance")
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
        "feature_type": feature_type,
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
        "source_type": source_type,
    }
