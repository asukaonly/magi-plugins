"""Normalize explicitly shared health observations without reading host devices."""
from __future__ import annotations
import time
from typing import Any

from magi_plugin_sdk import (
    ActivityFacet, ContentBlock, Source, SourceActivity, SourceChangeBatch,
    SourceMemoryPolicy, SourceNarration, SourceOutput, SourceSyncContext,
)

from .collection_contract import HealthRecord


class AppleHealthSource(Source):
    source_id = "timeline.apple_health"
    source_type = "apple_health"
    display_name = "Apple Health"
    supports_pull_sync = True
    polling_mode = "manual"
    update_key_fields = ("record_id",)
    memory_policy = SourceMemoryPolicy(allow_llm_extraction=False, importance_bias=0.3)

    def __init__(self, *, consent: bool = False) -> None:
        super().__init__()
        self._consent = consent

    def _require_consent(self) -> None:
        if not self._consent:
            raise PermissionError("Health data processing requires explicit consent")

    async def collect_items(self, context: SourceSyncContext) -> SourceChangeBatch:
        # A service host cannot collect HealthKit. A paired native collector owns it.
        self._require_consent()
        return SourceChangeBatch(changes=[], complete=True, stats={"requires_device": True})

    async def fetch_item(self, item: dict[str, Any]) -> dict[str, Any]:
        self._require_consent()
        return HealthRecord.model_validate(item).model_dump()

    async def build_output(self, item: dict[str, Any]) -> SourceOutput:
        self._require_consent()
        record = HealthRecord.model_validate(item)
        kind = self.t(f"types.{record.record_type}", fallback=record.record_type)
        unit = self.t(f"units.{record.unit}", fallback=record.unit)
        title = f"{kind}: {record.value:g} {unit}"
        if record.category:
            title += f" ({record.category})"
        body = self.t("record", fallback="{title}. Recorded by {source}; time zone {timezone}.",
                      title=title, source=record.source, timezone=record.timezone)
        return SourceOutput(
            source_type=self.source_type, source_item_id=record.record_id,
            occurred_at=record.start, captured_at=time.time(),
            activity=SourceActivity(
                source=ActivityFacet(code="apple_health", i18n_key="source", fallback="Apple Health"),
                action=ActivityFacet(code=record.record_type, i18n_key=f"types.{record.record_type}", fallback=kind),
                qualifiers={"value": record.value, "unit": record.unit}),
            narration=SourceNarration(title=title, body=body),
            content_blocks=[ContentBlock(kind="text", value=body)],
            provenance={"provider": "healthkit", "record_id": record.record_id, "source": record.source},
            domain_payload=record.model_dump(), tags=["health", record.record_type],
        )
