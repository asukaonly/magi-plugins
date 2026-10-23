"""Shared mail source; provider clients never escape into the host memory layer."""
from __future__ import annotations

import time
from typing import Protocol
from magi_plugin_sdk.runtime import SourceChangeBatch
from magi_plugin_sdk.source_query import SourceQueryRequest, SourceQueryResult
from magi_plugin_sdk.sources import (
    ActivityFacet, Source, SourceActivity, SourceMemoryPolicy, SourceNarration,
    SourceOutput, SourceSyncContext, TimelinePresentation,
)
from .models import MailMessage


class MailProvider(Protocol):
    async def collect(self, context: SourceSyncContext) -> SourceChangeBatch: ...
    async def query(self, request: SourceQueryRequest) -> SourceQueryResult: ...


class MailSource(Source):
    """One source per connected mailbox; mail content is external evidence."""
    source_id = "mail"
    source_type = "mail"
    display_name = "Mail"
    supports_pull_sync = True
    polling_mode = "interval"
    default_interval = 10
    update_key_fields = ("object_id",)
    memory_policy = SourceMemoryPolicy(
        author_type="external", content_type="observation", ingest_target="l1_only",
        cognition_eligible=False, allow_llm_extraction=False,
    )

    def __init__(self, provider: MailProvider) -> None:
        super().__init__()
        self.provider = provider

    async def collect_items(self, context: SourceSyncContext) -> SourceChangeBatch:
        return await self.provider.collect(context)

    async def query_source(self, request: SourceQueryRequest) -> SourceQueryResult:
        return await self.provider.query(request)

    async def build_output(self, item: dict) -> SourceOutput:
        message = MailMessage.model_validate(item)
        evidence = message.evidence()
        return SourceOutput(
            source_type="mail", source_item_id=message.object_id,
            occurred_at=message.occurred_at, captured_at=time.time(),
            activity=SourceActivity(
                source=ActivityFacet(code="mail", i18n_key="activity.source.mail", fallback="Mail"),
                action=ActivityFacet(code="received", i18n_key="activity.action.received", fallback="Mail message")),
            narration=SourceNarration(title=message.subject, body=evidence.text),
            domain_payload={**evidence.metadata, "thread_id": message.thread_id,
                "source_content_fields": evidence.fields, "url": message.url},
            timeline_presentation=TimelinePresentation(mode="evidence_only", title=message.subject),
        )
