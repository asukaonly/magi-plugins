"""Screenshot Timeline plugin entry point."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from magi_plugin_sdk import (
    Plugin,
    PluginSettingsActionResult,
    PluginSettingsActionSpec,
    PluginSettingsResourceSpec,
)
from magi_plugin_sdk.sources import SourceSpec

from .privacy_guard import DEFAULT_APP_BLOCKLIST
from .screenshot_tools import (
    build_recall_asset_refs as _build_recall_asset_refs,
    build_screenshot_timeline_tool_classes,
)
from .source import ScreenshotSource

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "capture_scope": "hybrid",
    "active_window_interval_sec": 10,
    "full_screen_interval_min": 5,
    # Order matters. VNRecognizeTextRequest.recognitionLanguages treats the
    # first entry as the PRIMARY character-set hypothesis — when set to
    # "en-US" first, Chinese glyphs get mapped onto Latin alphabet best-
    # matches and come out as gibberish like "ŁŘä*# #šùÑ£Ж?È=£". Putting
    # zh-Hans first uses Chinese as primary, English in code/URLs/brand
    # names still falls back cleanly. For an all-English UI the cost is
    # negligible (Latin tokens never look like CJK glyphs to the model).
    "ocr_languages": ["zh-Hans", "en-US"],
    "ocr_level": "accurate",
    # AX-first content extraction. When on, the helper reads the focused
    # window's accessibility tree and skips OCR when it's text-rich; hollow
    # trees (WeChat/QQ/games) fall back to OCR. `ax_wake` sends the private
    # AXManualAccessibility signal that makes Chromium/Electron apps expose
    # their content (off = native apps only). Thresholds gate the AX-vs-OCR
    # decision; the hollow-vs-rich gap is ~100x so they're forgiving.
    "ax_enabled": True,
    "ax_wake": True,
    "ax_min_content_chars": 80,
    "ax_min_content_nodes": 5,
    "original_retention_days": 30,
    "app_blocklist": list(DEFAULT_APP_BLOCKLIST),
    "window_title_blocklist": [],
    "thumbnail_max_width": 1024,
    "jpeg_quality_original": 80,
    "jpeg_quality_thumbnail": 70,
    "initial_sync_configured": False,
    "sync_mode": "interval",
}


class ScreenshotTimelinePlugin(Plugin):
    """Captures screen content with local OCR and feeds magi L1."""

    def __init__(self) -> None:
        super().__init__()
        # Track sources we created so `shutdown()` can stop them. The host
        # calls `get_sources()` once on load and the same instances are
        # retained in the SourceRegistry until unload — caching here lets
        # us tear them down on reload without poking the registry.
        self._owned_sources: list[ScreenshotSource] = []

    def get_sources(self) -> list[tuple[str, Any, SourceSpec]]:
        settings: dict[str, Any] = {}
        sources_settings = self.settings.get("sources", {})
        if isinstance(sources_settings, dict):
            # The YAML key must match `metadata.source_type` below — the host
            # scheduler reads `sources.<source_type>.enabled` to decide whether
            # to schedule this contribution. Keep them aligned.
            settings = dict(sources_settings.get("screenshot_timeline", {}))

        plugin_dir = Path(__file__).resolve().parent
        helper_argv = [str(plugin_dir / "bin" / "magi-vision-helper")]

        def _tuple(value: Any, default: Any) -> tuple:
            if isinstance(value, (list, tuple)):
                return tuple(value)
            return tuple(default)

        source = ScreenshotSource(
            helper_argv=helper_argv,
            resources_root=self.context.resources_dir,
            session_db_path=self.context.resources_dir / "sessions.db",
            retention_days=int(settings.get("original_retention_days", DEFAULT_SETTINGS["original_retention_days"])),
            capture_scope=str(settings.get("capture_scope", DEFAULT_SETTINGS["capture_scope"])),
            ocr_languages=_tuple(settings.get("ocr_languages"), DEFAULT_SETTINGS["ocr_languages"]),
            ocr_level=str(settings.get("ocr_level", DEFAULT_SETTINGS["ocr_level"])),
            ax_enabled=bool(settings.get("ax_enabled", DEFAULT_SETTINGS["ax_enabled"])),
            ax_wake=bool(settings.get("ax_wake", DEFAULT_SETTINGS["ax_wake"])),
            ax_min_content_chars=int(settings.get("ax_min_content_chars", DEFAULT_SETTINGS["ax_min_content_chars"])),
            ax_min_content_nodes=int(settings.get("ax_min_content_nodes", DEFAULT_SETTINGS["ax_min_content_nodes"])),
            extra_app_blocklist=_tuple(settings.get("app_blocklist"), DEFAULT_SETTINGS["app_blocklist"]),
            window_title_blocklist=_tuple(settings.get("window_title_blocklist"), DEFAULT_SETTINGS["window_title_blocklist"]),
            thumbnail_max_width=int(settings.get("thumbnail_max_width", DEFAULT_SETTINGS["thumbnail_max_width"])),
            jpeg_quality_original=int(settings.get("jpeg_quality_original", DEFAULT_SETTINGS["jpeg_quality_original"])),
            jpeg_quality_thumbnail=int(settings.get("jpeg_quality_thumbnail", DEFAULT_SETTINGS["jpeg_quality_thumbnail"])),
            active_window_interval_sec=float(settings.get("active_window_interval_sec", DEFAULT_SETTINGS["active_window_interval_sec"])),
            full_screen_interval_min=float(settings.get("full_screen_interval_min", DEFAULT_SETTINGS["full_screen_interval_min"])),
        )
        self._owned_sources.append(source)

        return [
            (
                "timeline.screenshot",
                source,
                SourceSpec(
                    source_id="timeline.screenshot",
                    display_name="Screenshot Timeline",
                    description="Continuous screen capture + local OCR fed into magi memory.",
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
                        "source_type": "screenshot_timeline",
                        "default_settings": {
                            field.key.rsplit(".", 1)[-1]: field.model_copy(deep=True).default
                            for field in self.manifest.settings_fields
                            if field.key.startswith("sources.") and field.type != "secret"
                        },
                        "activation_flow": self.manifest.activation_flow.model_dump() if self.manifest.activation_flow is not None else None,
                        "settings_ui_blocks": [
                            block.model_dump() for block in self.manifest.settings_ui_blocks
                        ],
                    },
                ),
            )
        ]

    def get_tools(self) -> list[type[Any]]:
        """Expose the capture-ref resolver tool to the chat LLM.

        Required because memory_query returns screenshot events with
        ``asset_ref_id = capture_id`` and tags them as needing
        ``screenshot_timeline_resolve_capture_refs`` to be resolved.
        Without this tool registered the LLM would mis-route those
        refs to photo_library's resolver (which doesn't know our IDs).
        """
        resources_root = self.context.resources_dir
        return build_screenshot_timeline_tool_classes(resources_root=resources_root, connection_id=self.connection.connection_id)

    def build_recall_artifacts(
        self,
        *,
        events: list[dict[str, Any]],
        query: str,
        query_mode: str | None,
    ) -> dict[str, Any] | None:
        """Project screenshot_timeline events into LLM-facing asset_refs.

        The host calls this on every memory_query so each plugin can
        translate its own L1 rows into a consistent ``asset_refs``
        envelope. We attach ``resolver_tool=screenshot_timeline_resolve_capture_refs``
        so the chat LLM picks the right resolver for our captures.

        See screenshot_tools.build_recall_asset_refs for the per-event
        projection logic.
        """
        _ = query, query_mode
        if not events:
            return None
        asset_refs: list[dict[str, Any]] = []
        for event in events:
            asset_refs.extend(_build_recall_asset_refs(event))
        if not asset_refs:
            return None
        return {"asset_refs": asset_refs}

    def get_settings_resources(self) -> list[PluginSettingsResourceSpec]:
        return [entry.model_copy(deep=True) for entry in self.manifest.settings_resources]

    def read_settings_resource(self, resource_name: str) -> Any:
        if resource_name != "permissions":
            raise KeyError(resource_name)
        from .permissions import all_statuses

        statuses = all_statuses()
        return {
            "items": [
                {
                    "id": "screen_recording",
                    "label": "Screen Recording",
                    "label_i18n_key": "screenshot_timeline.permissions.screen_recording.label",
                    "description": "Required to capture screen content.",
                    "description_i18n_key": "screenshot_timeline.permissions.screen_recording.description",
                    "status": statuses["screen_recording"],
                    "required": True,
                    "settings_url": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
                },
                {
                    "id": "accessibility",
                    "label": "Accessibility",
                    "label_i18n_key": "screenshot_timeline.permissions.accessibility.label",
                    "description": "Provides window titles and structured on-screen text (optional).",
                    "description_i18n_key": "screenshot_timeline.permissions.accessibility.description",
                    "status": statuses["accessibility"],
                    "required": False,
                    "settings_url": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
                },
            ],
        }

    def get_settings_actions(self) -> list[PluginSettingsActionSpec]:
        return [entry.model_copy(deep=True) for entry in self.manifest.settings_actions]

    async def start_settings_action(
        self,
        action_id: str,
        *,
        session_id: str,
        field_values: dict | None = None,
    ) -> PluginSettingsActionResult:
        if action_id != "request_permissions":
            raise KeyError(action_id)
        from .permissions import request_accessibility, request_screen_recording

        screen = request_screen_recording()
        accessibility = request_accessibility()
        parts = [
            f"Screen Recording: {screen}",
            f"Accessibility: {accessibility}",
        ]
        if screen == "granted":
            status = "succeeded"
            message = "✓ " + " · ".join(parts)
        else:
            status = "failed"
            message = (
                "✗ " + " · ".join(parts) +
                ". If you denied earlier, grant manually in System Settings → "
                "Privacy & Security → Screen Recording."
            )
        return PluginSettingsActionResult(
            status=status,
            message=message,
            data={
                "permissions": {
                    "screen_recording": screen,
                    "accessibility": accessibility,
                }
            },
        )

    async def shutdown(self) -> None:
        """Stop the screenshot source and its helper subprocess on unload.

        The host calls this on reload (settings change, disable, upgrade).
        Without it, every reload leaks the previous source: its timer
        keeps ticking, its NSWorkspace observer keeps listening, and its
        helper subprocess keeps consuming memory + battery. The visible
        symptom is "I set the interval to 120s and now captures fire
        every 3s" — actually multiple source instances stacking up.
        """
        # Snapshot + clear up front so a re-entrant call is a no-op.
        owned = list(self._owned_sources)
        self._owned_sources.clear()
        for source in owned:
            try:
                await source.stop()
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "plugin.source_stop_failed source=%r",
                    getattr(source, "source_type", source),
                )


__all__ = ["ScreenshotTimelinePlugin", "DEFAULT_SETTINGS"]
