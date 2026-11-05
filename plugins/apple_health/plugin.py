"""Service processing for the bundled iOS Apple Health collector."""
from __future__ import annotations
from magi_plugin_sdk import Plugin, SourceSpec

from .collection_contract import collection_capability
from .source import AppleHealthSource


class AppleHealthPlugin(Plugin):
    def get_sources(self) -> list[tuple]:
        if self.manifest is None:
            raise RuntimeError("Plugin must be configured before registration")
        settings = self.settings.get("sources", {}).get("apple_health", {})
        source = AppleHealthSource(consent=settings.get("processing_consent") is True)
        return [(source.source_id, source, SourceSpec(
            source_id=source.source_id, display_name=self.i18n.t("source"),
            domain="timeline", surface="timeline", sync_mode="manual", polling_mode="manual",
            fields=[field.model_copy(deep=True) for field in sorted(self.manifest.settings_fields, key=lambda field: field.order)
                    if field.surface == "timeline" and field.section != "activation"],
            metadata={
                "source_type": source.source_type,
                "remote_collection": collection_capability(),
                "default_settings": {field.key.rsplit(".", 1)[-1]: field.model_copy(deep=True).default
                                     for field in self.manifest.settings_fields if field.key.startswith("sources.")},
                "activation_flow": self.manifest.activation_flow.model_dump() if self.manifest.activation_flow else None,
            },
        ))]
