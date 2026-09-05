"""Resolver tools shared by the local and Apple photo plugins."""
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

from .apple_photos_reader import (
    APPLE_PHOTOS_ASSET_PREFIX,
    ApplePhotosReader,
    ApplePhotosReaderError,
)
from .reader import PhotoLibraryReader


LOCAL_PHOTOS_RESOLVER_TOOL = "local_photos_resolve_photo_refs"
APPLE_PHOTOS_RESOLVER_TOOL = "apple_photos_resolve_photo_refs"


def build_local_photo_tool_classes(settings: dict[str, Any], *, connection_id: str) -> list[type[Tool]]:
    """Build the resolver tool for local photo folders."""
    return _build_photo_tool_classes(
        settings,
        connection_id=connection_id,
        source_mode="directory",
        tool_name=LOCAL_PHOTOS_RESOLVER_TOOL,
    )


def build_apple_photo_tool_classes(settings: dict[str, Any], *, connection_id: str) -> list[type[Tool]]:
    """Build the resolver tool for Apple Photos."""
    return _build_photo_tool_classes(
        settings,
        connection_id=connection_id,
        source_mode="apple_photos",
        tool_name=APPLE_PHOTOS_RESOLVER_TOOL,
    )


def _build_photo_tool_classes(
    settings: dict[str, Any],
    *,
    source_mode: str,
    connection_id: str,
    tool_name: str,
) -> list[type[Tool]]:
    """Build a configured resolver for exactly one installed photo source."""
    photos_library_path = str(settings.get("photos_library_path", "") or "").strip()
    source_paths = _resolve_source_paths(settings)
    exclude_patterns = _resolve_string_list(settings.get("exclude_patterns"))
    analysis_features = _resolve_string_list(settings.get("analysis_features")) or ["exif"]

    class PhotoLibraryResolvePhotoRefsTool(Tool):
        _source_mode = source_mode
        _tool_name = tool_name
        _photos_library_path = photos_library_path
        _source_paths = list(source_paths)
        _exclude_patterns = list(exclude_patterns)
        _analysis_features = list(analysis_features)
        _reader_factory = PhotoLibraryReader
        _apple_reader_factory = ApplePhotosReader

        def __init__(self) -> None:
            self._reader = self._reader_factory()
            self._apple_reader = self._apple_reader_factory()
            super().__init__()

        def _init_schema(self) -> None:
            self.schema = ToolSchema(
                name=self._tool_name,
                description=(
                    "Resolve photo asset refs returned by memory_query back to current local file paths "
                    "so the host can prepare chat attachments for sending."
                ),
                category="photos",
                effect_class="read_only",
                effect_replay_policy="read_only",
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

            apple_ids = (
                [ref_id for ref_id in requested_ids if ref_id.startswith(APPLE_PHOTOS_ASSET_PREFIX)]
                if self._source_mode == "apple_photos"
                else []
            )
            directory_ids = (
                [ref_id for ref_id in requested_ids if not ref_id.startswith(APPLE_PHOTOS_ASSET_PREFIX)]
                if self._source_mode == "directory"
                else []
            )

            resolved_by_id: dict[str, dict[str, Any]] = {}
            directory_config_missing = False

            if apple_ids:
                try:
                    apple_items, _ = self._apple_reader.resolve_asset_refs(
                        apple_ids,
                        self._photos_library_path,
                    )
                except ApplePhotosReaderError as exc:
                    return ToolResult(
                        success=False,
                        error=str(exc),
                        error_code=ToolErrorCode.INVALID_CONFIG.value,
                    )
                for item in apple_items:
                    resolved_by_id[_asset_ref_id(item)] = item

            if directory_ids:
                if not self._source_paths:
                    if not apple_ids:
                        return ToolResult(
                            success=False,
                            error="photo_library source_paths are not configured.",
                            error_code=ToolErrorCode.INVALID_CONFIG.value,
                        )
                    directory_config_missing = True
                else:
                    items = _scan_photo_items(
                        reader=self._reader,
                        source_paths=self._source_paths,
                        exclude_patterns=self._exclude_patterns,
                        analysis_features=self._analysis_features,
                        min_modified_at=0.0,
                        max_scan_items=max(len(directory_ids) * 200, 1000),
                    )
                    indexed = {_asset_ref_id(item): item for item in items}
                    for ref_id in directory_ids:
                        item = indexed.get(ref_id)
                        if item is not None:
                            resolved_by_id[ref_id] = item

            resolved_items = []
            missing_ids = []
            for ref_id in requested_ids:
                item = resolved_by_id.get(ref_id)
                if item is None:
                    missing_ids.append(ref_id)
                else:
                    resolved_items.append(item)

            resolved_refs = [
                _build_asset_ref(
                    item,
                    resolver_tool=self._tool_name,
                    resolution_state="resolved",
                    connection_id=connection_id,
                    source_type="photo_library_apple_photos" if source_mode == "apple_photos" else "photo_library_directory",
                )
                for item in resolved_items
            ]
            file_paths = [str(item.get("path") or "") for item in resolved_items if str(item.get("path") or "")]

            summary = f"Resolved {len(file_paths)} photo asset(s)."
            if missing_ids:
                summary += f" {len(missing_ids)} asset ref(s) could not be resolved."
            if directory_config_missing:
                summary += " photo_library source_paths are not configured; skipped non-Apple Photos refs."
            summary += " Call prepare_chat_attachments with file_paths to send them in chat."

            return ToolResult(
                success=True,
                data={
                    "asset_refs": resolved_refs,
                    "assistant_payload": {"asset_refs": resolved_refs},
                    "file_paths": file_paths,
                    "resolved_count": len(file_paths),
                    "missing_asset_ref_ids": missing_ids,
                    "summary": summary,
                },
            )

    return [PhotoLibraryResolvePhotoRefsTool]


def _resolve_source_paths(settings: dict[str, Any]) -> list[str]:
    raw_paths = settings.get("source_paths")
    if isinstance(raw_paths, list):
        return [str(item) for item in raw_paths if str(item or "").strip()]
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
    resolver_tool: str,
    connection_id: str,
    source_type: str,
    resolution_state: str | None = None,
) -> dict[str, Any]:
    asset_ref: dict[str, Any] = {
        "asset_ref_id": _asset_ref_id(item),
        "source_type": source_type,
        "connection_id": connection_id,
        "source_item_id": str(item.get("asset_local_id") or "").strip() or None,
        "original_name": str(item.get("filename") or "").strip() or None,
        "display_name": str(item.get("filename") or "").strip() or None,
        "captured_at": float(item.get("capture_timestamp") or item.get("modified_at") or 0.0),
        "kind": "image",
        "resolver_tool": resolver_tool,
    }
    if resolution_state is not None:
        asset_ref["resolution_state"] = resolution_state
    return {
        key: value
        for key, value in asset_ref.items()
        if value not in (None, "", [], {})
    }
