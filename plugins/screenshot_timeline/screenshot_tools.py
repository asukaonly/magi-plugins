"""Resolver tool — turn a capture_id back into a local jpg path.

memory_query returns screenshot captures to the chat LLM with an
``asset_ref_id`` of the form ``20260528T150647_328216_54AQ`` (the
capture_id). The LLM can't send the jpg directly — it needs to learn
the on-disk path and hand it to ``prepare_chat_attachments``.

This tool is that bridge. Given a list of capture_ids, it:
  1. Decodes the date out of the timestamp prefix.
  2. Reconstructs the canonical paths
       <resources_root>/originals/YYYY/MM/DD/<id>.jpg
       <resources_root>/thumbnails/YYYY/MM/DD/<id>.jpg
  3. Verifies the file exists on disk (skips silently otherwise — the
     original may have aged out of retention, in which case only the
     thumbnail remains usable).
  4. Returns ``file_paths`` so the LLM can call
     ``prepare_chat_attachments(file_paths=...)`` next.

Mirrors photo_library_resolve_photo_refs' interface so the chat LLM
uses the same calling pattern across sensors.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from magi_plugin_sdk.tools import (
    ParameterType,
    Tool,
    ToolErrorCode,
    ToolExecutionContext,
    ToolParameter,
    ToolResult,
    ToolSchema,
)


def build_screenshot_timeline_tool_classes(
    resources_root: Path,
) -> list[type[Tool]]:
    """Build the configured resolver tool class for the current plugin."""

    class ScreenshotTimelineResolveCaptureRefsTool(Tool):
        _resources_root = Path(resources_root)

        def _init_schema(self) -> None:
            self.schema = ToolSchema(
                name="screenshot_timeline_resolve_capture_refs",
                description=(
                    "Resolve screenshot_timeline capture refs returned by "
                    "memory_query back to current local jpg paths so the host "
                    "can prepare chat attachments. Pass the asset_ref_ids whose "
                    "source_type is 'screenshot_timeline'. Returns file_paths "
                    "that should then be passed to prepare_chat_attachments."
                ),
                category="screenshots",
                parameters=[
                    ToolParameter(
                        name="capture_ref_ids",
                        type=ParameterType.ARRAY,
                        required=True,
                        array_item_type=ParameterType.STRING,
                        description=(
                            "Capture ids previously returned by memory_query as "
                            "asset_ref_id for events with source=screenshot_timeline."
                        ),
                    ),
                    ToolParameter(
                        name="prefer_thumbnail",
                        type=ParameterType.BOOLEAN,
                        required=False,
                        description=(
                            "If true, return the thumbnail path even when the "
                            "original is still on disk. Defaults to false "
                            "(originals preferred while still in retention)."
                        ),
                    ),
                ],
            )

        async def execute(
            self,
            parameters: dict[str, Any],
            context: ToolExecutionContext,
        ) -> ToolResult:
            _ = context
            capture_ref_ids = parameters.get("capture_ref_ids")
            if not isinstance(capture_ref_ids, list) or not capture_ref_ids:
                return ToolResult(
                    success=False,
                    error="capture_ref_ids must be a non-empty list.",
                    error_code=ToolErrorCode.INVALID_PARAMETERS.value,
                )
            requested_ids = [
                str(item or "").strip()
                for item in capture_ref_ids
                if str(item or "").strip()
            ]
            if not requested_ids:
                return ToolResult(
                    success=False,
                    error="capture_ref_ids must contain at least one non-empty id.",
                    error_code=ToolErrorCode.INVALID_PARAMETERS.value,
                )

            prefer_thumbnail = bool(parameters.get("prefer_thumbnail", False))
            root = self._resources_root

            asset_refs: list[dict[str, Any]] = []
            file_paths: list[str] = []
            missing_ids: list[str] = []
            for capture_id in requested_ids:
                date_subpath = _date_subpath_from_capture_id(capture_id)
                if date_subpath is None:
                    missing_ids.append(capture_id)
                    continue
                original = root / "originals" / date_subpath / f"{capture_id}.jpg"
                thumbnail = root / "thumbnails" / date_subpath / f"{capture_id}.jpg"
                has_original = original.is_file()
                has_thumbnail = thumbnail.is_file()
                if not has_original and not has_thumbnail:
                    missing_ids.append(capture_id)
                    continue

                # Pick which path to surface to the host for attachment.
                if prefer_thumbnail and has_thumbnail:
                    chosen = thumbnail
                elif has_original:
                    chosen = original
                else:
                    chosen = thumbnail
                file_paths.append(str(chosen))

                asset_refs.append(
                    {
                        "asset_ref_id": capture_id,
                        "source_type": "screenshot_timeline",
                        "source_item_id": capture_id,
                        "kind": "image",
                        "resolver_tool": "screenshot_timeline_resolve_capture_refs",
                        "resolution_state": "resolved",
                        "file_path": str(chosen),
                        "original_path": str(original) if has_original else None,
                        "thumbnail_path": str(thumbnail) if has_thumbnail else None,
                    }
                )

            asset_refs = [
                {k: v for k, v in r.items() if v is not None}
                for r in asset_refs
            ]
            return ToolResult(
                success=True,
                data={
                    "asset_refs": asset_refs,
                    "assistant_payload": {"asset_refs": asset_refs},
                    "file_paths": file_paths,
                    "resolved_count": len(file_paths),
                    "missing_capture_ref_ids": missing_ids,
                    "summary": (
                        f"Resolved {len(file_paths)} screenshot(s). "
                        "Call prepare_chat_attachments with file_paths to send "
                        "them in chat."
                    ),
                },
            )

    return [ScreenshotTimelineResolveCaptureRefsTool]


def _date_subpath_from_capture_id(capture_id: str) -> str | None:
    """Extract ``YYYY/MM/DD`` from a capture_id like ``20260528T150647_...``.

    Returns None on anything that doesn't match the format — we never
    want to invent a path and probe random parts of the filesystem.
    """
    if not capture_id or len(capture_id) < 8:
        return None
    yyyymmdd = capture_id[:8]
    if not yyyymmdd.isdigit():
        return None
    # Cheap shape validation — full datetime parse is overkill here.
    year = yyyymmdd[0:4]
    month = yyyymmdd[4:6]
    day = yyyymmdd[6:8]
    try:
        month_i = int(month)
        day_i = int(day)
    except ValueError:
        return None
    if not (1 <= month_i <= 12 and 1 <= day_i <= 31):
        return None
    return f"{year}/{month}/{day}"


def build_recall_asset_refs(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Project one screenshot_timeline L1 event into asset_ref(s).

    Called from the plugin's ``build_recall_artifacts`` hook. The result
    rides along inside memory_query's response so the chat LLM sees:

        {
          "asset_ref_id": "<capture_id>",
          "source_type": "screenshot_timeline",
          "resolver_tool": "screenshot_timeline_resolve_capture_refs",
          ...
        }

    The LLM then knows: "this is a screenshot asset; the right resolver
    is screenshot_timeline_resolve_capture_refs (NOT
    photo_library_resolve_photo_refs)". That avoids the cross-plugin
    misrouting that motivated this tool.
    """
    if not isinstance(event, dict):
        return []
    source = str(event.get("source") or "").strip()
    if source != "screenshot_timeline":
        return []

    source_item_id = str(event.get("source_item_id") or "").strip()
    if not source_item_id:
        return []

    metadata = event.get("metadata_json") if isinstance(event.get("metadata_json"), dict) else {}
    timeline = metadata.get("timeline") if isinstance(metadata.get("timeline"), dict) else {}
    provenance = timeline.get("provenance") if isinstance(timeline.get("provenance"), dict) else {}
    activity = metadata.get("activity") if isinstance(metadata.get("activity"), dict) else {}
    qualifiers = activity.get("qualifiers") if isinstance(activity.get("qualifiers"), dict) else {}

    app_name = str(qualifiers.get("app_name") or "").strip() or None
    window_title = str(qualifiers.get("window_title") or "").strip() or None
    display_name = " · ".join(part for part in (app_name, window_title) if part) or None

    asset_ref = {
        "asset_ref_id": source_item_id,
        "kind": "image",
        "event_id": str(event.get("event_id") or "").strip() or None,
        "source_type": "screenshot_timeline",
        "source_item_id": source_item_id,
        "display_name": display_name,
        "captured_at": (
            provenance.get("captured_at")
            or timeline.get("captured_at")
            or event.get("timestamp")
        ),
        "occurred_at": event.get("timestamp") or event.get("created_at"),
        "resolver_tool": "screenshot_timeline_resolve_capture_refs",
    }
    return [
        {key: value for key, value in asset_ref.items() if value not in (None, "")}
    ]


__all__ = [
    "build_screenshot_timeline_tool_classes",
    "build_recall_asset_refs",
]
