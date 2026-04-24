"""Resolver tools for the photo-library plugin."""
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

from .reader import PhotoLibraryReader


def build_photo_library_tool_classes(settings: dict[str, Any]) -> list[type[Tool]]:
    """Build configured tool classes for the current plugin settings."""
    source_paths = _resolve_source_paths(settings)
    exclude_patterns = _resolve_string_list(settings.get("exclude_patterns"))
    analysis_features = _resolve_string_list(settings.get("analysis_features")) or ["exif"]

    class PhotoLibraryResolvePhotoRefsTool(Tool):
        _source_paths = list(source_paths)
        _exclude_patterns = list(exclude_patterns)
        _analysis_features = list(analysis_features)
        _reader_factory = PhotoLibraryReader

        def __init__(self) -> None:
            self._reader = self._reader_factory()
            super().__init__()

        def _init_schema(self) -> None:
            self.schema = ToolSchema(
                name="photo_library_resolve_photo_refs",
                description=(
                    "Resolve photo-library asset refs returned by memory_query back to current local file paths "
                    "so the host can prepare chat attachments for sending."
                ),
                category="photos",
                parameters=[
                    ToolParameter(
                        name="asset_ref_ids",
                        type=ParameterType.ARRAY,
                        required=True,
                        array_item_type=ParameterType.STRING,
                        description="Asset reference ids previously returned by memory_query for photo_library assets.",
                    ),
                ],
            )

        async def execute(
            self,
            parameters: dict[str, Any],
            context: ToolExecutionContext,
        ) -> ToolResult:
            _ = context
            if not self._source_paths:
                return ToolResult(
                    success=False,
                    error="photo_library source_paths are not configured.",
                    error_code=ToolErrorCode.INVALID_CONFIG.value,
                )

            asset_ref_ids = parameters.get("asset_ref_ids")
            if not isinstance(asset_ref_ids, list) or not asset_ref_ids:
                return ToolResult(
                    success=False,
                    error="asset_ref_ids must be a non-empty list.",
                    error_code=ToolErrorCode.INVALID_PARAMETERS.value,
                )

            requested_ids = [str(item or "").strip() for item in asset_ref_ids if str(item or "").strip()]
            if not requested_ids:
                return ToolResult(
                    success=False,
                    error="asset_ref_ids must contain at least one non-empty id.",
                    error_code=ToolErrorCode.INVALID_PARAMETERS.value,
                )

            items = _scan_photo_items(
                reader=self._reader,
                source_paths=self._source_paths,
                exclude_patterns=self._exclude_patterns,
                analysis_features=self._analysis_features,
                min_modified_at=0.0,
                max_scan_items=max(len(requested_ids) * 200, 1000),
            )
            indexed = {_asset_ref_id(item): item for item in items}

            resolved_items: list[dict[str, Any]] = []
            missing_ids: list[str] = []
            for asset_ref_id in requested_ids:
                item = indexed.get(asset_ref_id)
                if item is None:
                    missing_ids.append(asset_ref_id)
                    continue
                resolved_items.append(item)

            resolved_refs = [
                _build_asset_ref(item, resolution_state="resolved")
                for item in resolved_items
            ]
            file_paths = [str(item.get("path") or "") for item in resolved_items if str(item.get("path") or "")]

            return ToolResult(
                success=True,
                data={
                    "asset_refs": resolved_refs,
                    "assistant_payload": {"asset_refs": resolved_refs},
                    "file_paths": file_paths,
                    "resolved_count": len(file_paths),
                    "missing_asset_ref_ids": missing_ids,
                    "summary": (
                        f"Resolved {len(file_paths)} photo asset(s). "
                        "Call prepare_chat_attachments with file_paths to send them in chat."
                    ),
                },
            )

    return [PhotoLibraryResolvePhotoRefsTool]


def _resolve_source_paths(settings: dict[str, Any]) -> list[str]:
    raw_paths = settings.get("source_paths")
    if isinstance(raw_paths, list):
        return [str(item) for item in raw_paths if str(item or "").strip()]
    legacy = settings.get("source_path")
    if str(legacy or "").strip():
        return [str(legacy).strip()]
    return []


def _resolve_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _scan_photo_items(
    *,
    reader: PhotoLibraryReader,
    source_paths: list[str],
    exclude_patterns: list[str],
    analysis_features: list[str],
    min_modified_at: float,
    max_scan_items: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    scan_limit = max(100, max_scan_items)
    for source_path in source_paths:
        result = reader.scan_directory(
            source_path,
            limit=scan_limit,
            min_modified_at=min_modified_at,
            exclude_patterns=exclude_patterns,
            analysis_features=analysis_features,
        )
        allowed_root = Path(source_path).expanduser().resolve()
        for item in result.items:
            item_path_raw = str(item.get("path") or "").strip()
            if not item_path_raw:
                continue
            try:
                item_path = Path(item_path_raw).expanduser().resolve()
            except OSError:
                continue
            if allowed_root in {item_path, *item_path.parents}:
                items.append(dict(item))
    return items
 

def _asset_ref_id(item: dict[str, Any]) -> str:
    raw = str(item.get("asset_local_id") or item.get("file_hash") or "").strip()
    if raw:
        return raw
    filename = str(item.get("filename") or "unknown")
    modified_at = int(float(item.get("modified_at") or 0.0))
    return f"fallback:{filename}:{modified_at}"


def _build_asset_ref(
    item: dict[str, Any],
    *,
    resolution_state: str | None = None,
) -> dict[str, Any]:
    asset_ref: dict[str, Any] = {
        "asset_ref_id": _asset_ref_id(item),
        "source_type": "photo_library",
        "source_item_id": str(item.get("asset_local_id") or "").strip() or None,
        "original_name": str(item.get("filename") or "").strip() or None,
        "display_name": str(item.get("filename") or "").strip() or None,
        "captured_at": float(item.get("capture_timestamp") or item.get("modified_at") or 0.0),
        "kind": "image",
        "resolver_tool": "photo_library_resolve_photo_refs",
    }
    if resolution_state is not None:
        asset_ref["resolution_state"] = resolution_state
    return {
        key: value
        for key, value in asset_ref.items()
        if value not in (None, "", [], {})
    }