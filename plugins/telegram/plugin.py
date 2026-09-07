"""Telegram channel plugin — wraps the Telegram channel adapter as a plugin contribution."""

from __future__ import annotations

from collections import Counter
from typing import Any

from magi_plugin_sdk import ExtensionFieldSpec, Plugin
from magi_plugin_sdk.channels import Channel

from .adapter import TelegramChannel, TelegramChannelConfig


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


def _telegram_metadata(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata_json")
    if not isinstance(metadata, dict):
        return {}
    if metadata.get("external_chat_id") or metadata.get("channel_type") == "telegram":
        return metadata
    channel = metadata.get("channel")
    if isinstance(channel, dict):
        return channel
    activity_snapshot = metadata.get("activity_snapshot")
    if isinstance(activity_snapshot, dict):
        provenance = activity_snapshot.get("provenance")
        if isinstance(provenance, dict):
            return provenance
    return metadata


class TelegramPlugin(Plugin):
    """Telegram bot channel plugin.

    Exposes a bidirectional Telegram bot as a channel contribution.
    Configure bot_token and other settings through the Extensions settings panel.
    """

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
        """Aggregate Telegram channel message features for L3 summaries."""
        _ = summary_category, period_start, period_end
        if source_type != "telegram" or not events:
            return None

        chat_counter: Counter[str] = Counter()
        group_count = 0
        direct_count = 0
        representative_event_ids: list[str] = []

        for event in events:
            channel_metadata = _telegram_metadata(event)
            chat_id = str(channel_metadata.get("external_chat_id") or "unknown").strip() or "unknown"
            chat_counter[chat_id] += 1
            if bool(channel_metadata.get("is_group")):
                group_count += 1
            else:
                direct_count += 1
            event_id = str(event.get("event_id") or "").strip()
            if event_id and len(representative_event_ids) < 8:
                representative_event_ids.append(event_id)

        covered_event_count = len(events)
        total_event_count = _budget_int(budget, "total_event_count", covered_event_count)
        omitted_event_count = max(0, total_event_count - covered_event_count)
        summary_lines = [
            f"Telegram feature coverage used {covered_event_count} messages across {len(chat_counter)} chats."
        ]
        if group_count or direct_count:
            summary_lines.append(f"Telegram messages split into {direct_count} direct messages and {group_count} group-triggered messages.")
        if omitted_event_count > 0:
            summary_lines.append(
                f"Telegram feature coverage used {covered_event_count} representative messages; {omitted_event_count} additional messages were compacted."
            )

        return {
            "feature_type": "telegram_channel",
            "message_count": covered_event_count,
            "total_event_count": total_event_count,
            "covered_event_count": covered_event_count,
            "omitted_event_count": omitted_event_count,
            "coverage_ratio": (covered_event_count / total_event_count) if total_event_count else None,
            "chat_count": len(chat_counter),
            "direct_message_count": direct_count,
            "group_message_count": group_count,
            "representative_event_ids": representative_event_ids,
            "summary_lines": summary_lines,
        }

    def get_channel(self) -> Channel:
        config = TelegramChannelConfig(
            bot_token=self.context.credentials.get("bot_token") or "",
            mode=self.settings.get("mode", "polling"),
            webhook_url=self.settings.get("webhook_url", ""),
            webhook_secret=self.context.credentials.get("webhook_secret") or "",
            proxy=self.settings.get("proxy", ""),
            allowed_user_ids=list(self.settings.get("allowed_user_ids") or []),
            group_trigger_keyword=self.settings.get("group_trigger_keyword", ""),
            magi_user_id=self.settings.get("magi_user_id", "default"),
            max_message_length=int(self.settings.get("max_message_length", 4096)),
        )
        return TelegramChannel(config=config)

    def get_channel_fields(self) -> list[ExtensionFieldSpec]:
        return [
            field.model_copy(deep=True)
            for field in sorted(self.manifest.settings_fields, key=lambda field: field.order)
            if field.surface == "extensions" and field.section != "advanced"
        ]
