"""Screenshot Timeline plugin entry point."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from magi_plugin_sdk import (
    ActivationFlowSpec,
    ExtensionFieldOption,
    ExtensionFieldSpec,
    Plugin,
    PluginSettingsActionResult,
    PluginSettingsActionSpec,
    PluginSettingsResourceSpec,
    SettingsUIBlockSpec,
)
from magi_plugin_sdk.sensors import SensorSpec

from .privacy_guard import DEFAULT_APP_BLOCKLIST
from .sensor import ScreenshotSensor

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "capture_scope": "hybrid",
    "active_window_interval_sec": 10,
    "full_screen_interval_min": 5,
    "ocr_languages": ["en-US", "zh-Hans"],
    "ocr_level": "accurate",
    "original_retention_days": 30,
    "keyboard_triggers_enabled": False,
    "keyboard_trigger_types": ["scroll", "arrow", "space", "delete"],
    "app_blocklist": list(DEFAULT_APP_BLOCKLIST),
    "window_title_blocklist": [],
    "panic_hotkey": "Option+Shift+P",
    "panic_pause_seconds": 60,
    "gap_minutes": 5,
    "max_minutes": 30,
    "thumbnail_max_width": 1024,
    "jpeg_quality_original": 80,
    "jpeg_quality_thumbnail": 70,
    "initial_sync_configured": False,
    "sync_mode": "interval",
}


def _activation_flow(prefix: str) -> ActivationFlowSpec:
    return ActivationFlowSpec(
        title="Enable Screenshot Timeline",
        description=(
            "This plugin continuously captures your screen and runs local OCR (via Apple Vision) "
            "to feed the recognised text into magi's memory. Captures and thumbnails stay on "
            "this Mac — they are not uploaded anywhere. Originals are deleted after 30 days "
            "by default; thumbnails are kept indefinitely.\n\n"
            "Password managers, Keychain, and incognito browser windows are skipped by default. "
            "You can add more app or window-title rules in Settings after enabling, and the "
            "panic hotkey (⌥⇧P) immediately pauses capture for 60 seconds.\n\n"
            "macOS will ask for Screen Recording permission the first time captures start. "
            "Accessibility permission is optional (only needed for keyboard triggers and the "
            "panic hotkey)."
        ),
        confirm_label="I understand — enable",
        cancel_label="Not now",
        enabled_key=f"{prefix}.enabled",
        configured_key=f"{prefix}.initial_sync_configured",
        fields=[],  # Capture scope / retention live in the regular settings panel.
    )


def _settings_ui_blocks(prefix: str) -> list[SettingsUIBlockSpec]:
    """Host-rendered custom blocks for the Screenshot Timeline plugin."""
    return [
        SettingsUIBlockSpec(
            block_id="macos_permissions",
            type="resource_picker",
            title="macOS Permissions",
            description=(
                "Screen Recording is required for capture. Accessibility is optional, "
                "needed only for keyboard triggers and the panic hotkey."
            ),
            resource_name="permissions",
            value_key="_readonly",
            presentation="permission_status",
        ),
    ]


def _fields(prefix: str) -> list[ExtensionFieldSpec]:
    return [
        ExtensionFieldSpec(
            key=f"{prefix}.enabled",
            type="switch",
            label="Enabled",
            description="Whether the screenshot timeline sensor is active.",
            default=False,
            section="general",
            surface="timeline",
            order=10,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.capture_scope",
            type="select",
            label="Capture scope",
            description="What part of the screen to capture each tick.",
            default="hybrid",
            options=[
                ExtensionFieldOption(label="Active window only", value="active_window"),
                ExtensionFieldOption(label="Primary display (full screen)", value="full_screen"),
                ExtensionFieldOption(label="Active window + periodic full screen", value="hybrid"),
                ExtensionFieldOption(label="All connected displays", value="all_displays"),
            ],
            section="capture",
            surface="timeline",
            order=20,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.active_window_interval_sec",
            type="number",
            label="Active window interval (sec)",
            description="How often to capture the active window. Recommended: 10s.",
            default=10,
            section="capture",
            surface="timeline",
            order=30,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.full_screen_interval_min",
            type="number",
            label="Full-screen interval (min)",
            description="Periodic full-screen capture interval (used in hybrid or full_screen modes).",
            default=5,
            section="capture",
            surface="timeline",
            order=40,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.ocr_languages",
            type="tags",
            label="OCR languages",
            description="Apple Vision recognition language codes (BCP-47), e.g. en-US, zh-Hans, ja.",
            default=["en-US", "zh-Hans"],
            section="ocr",
            surface="timeline",
            order=50,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.ocr_level",
            type="select",
            label="OCR recognition level",
            description=(
                "Apple Vision text-recognition precision tier. \"Accurate\" uses "
                "a neural network — handles small fonts, mixed-language text, "
                "and dense UI; ~0.5–2s per screenshot. \"Fast\" uses traditional "
                "character recognition — 5-10× faster but small fonts, CJK "
                "characters, and busy UI degrade quickly. Keep \"Accurate\" "
                "unless CPU is an issue."
            ),
            default="accurate",
            options=[
                ExtensionFieldOption(label="Accurate", value="accurate"),
                ExtensionFieldOption(label="Fast", value="fast"),
            ],
            section="ocr",
            surface="timeline",
            order=60,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.original_retention_days",
            type="number",
            label="Original retention (days)",
            description="Originals older than this are deleted. Thumbnails are kept permanently.",
            default=30,
            section="storage",
            surface="timeline",
            order=70,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.keyboard_triggers_enabled",
            type="switch",
            label="Enable keyboard triggers",
            description="Capture on scroll, arrow keys, space, or delete. Requires Accessibility permission.",
            default=False,
            section="triggers",
            surface="timeline",
            order=80,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.keyboard_trigger_types",
            type="tags",
            label="Keyboard trigger keys",
            description="Which keys trigger a capture. Has no effect unless keyboard triggers are enabled.",
            default=["scroll", "arrow", "space", "delete"],
            section="triggers",
            surface="timeline",
            order=90,
            depends_on_key=f"{prefix}.keyboard_triggers_enabled",
            depends_on_values=["true"],
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.app_blocklist",
            type="tags",
            label="App blocklist (bundle IDs)",
            description="Bundle IDs to never capture. Glob patterns supported (e.g. com.example.*). Defaults block known password managers and the macOS SecurityAgent; remove entries to allow them.",
            default=list(DEFAULT_APP_BLOCKLIST),
            section="privacy",
            surface="timeline",
            order=100,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.window_title_blocklist",
            type="tags",
            label="Window title blocklist (substrings)",
            description="Any window whose title contains any of these strings will be skipped.",
            default=[],
            section="privacy",
            surface="timeline",
            order=110,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.panic_hotkey",
            type="input",
            label="Panic hotkey",
            description="Press to immediately pause capture. Format: Modifier+Modifier+Key.",
            default="Option+Shift+P",
            placeholder="Option+Shift+P",
            section="privacy",
            surface="timeline",
            order=120,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.panic_pause_seconds",
            type="number",
            label="Panic pause duration (sec)",
            description="How long to pause capture after the panic hotkey is pressed.",
            default=60,
            section="privacy",
            surface="timeline",
            order=130,
        ),
        ExtensionFieldSpec(
            key=f"{prefix}.sync_mode",
            type="select",
            label="Sync mode",
            description="How the host should pull harvested bursts.",
            default="interval",
            options=[
                ExtensionFieldOption(label="Manual", value="manual"),
                ExtensionFieldOption(label="Interval", value="interval"),
            ],
            section="general",
            surface="timeline",
            order=140,
        ),
    ]


class ScreenshotTimelinePlugin(Plugin):
    """Captures screen content with local OCR and feeds magi L1."""

    def __init__(self) -> None:
        super().__init__()
        # Track sensors we created so `shutdown()` can stop them. The host
        # calls `get_sensors()` once on load and the same instances are
        # retained in the SensorRegistry until unload — caching here lets
        # us tear them down on reload without poking the registry.
        self._owned_sensors: list[ScreenshotSensor] = []

    def get_sensors(self) -> list[tuple[str, Any, SensorSpec]]:
        settings: dict[str, Any] = {}
        sensors_settings = self.settings.get("sensors", {})
        if isinstance(sensors_settings, dict):
            # The YAML key must match `metadata.source_type` below — the host
            # scheduler reads `sensors.<source_type>.enabled` to decide whether
            # to schedule this contribution. Keep them aligned.
            settings = dict(sensors_settings.get("screenshot_timeline", {}))

        plugin_dir = Path(__file__).resolve().parent
        helper_argv = [str(plugin_dir / "bin" / "magi-vision-helper")]

        def _tuple(value: Any, default: Any) -> tuple:
            if isinstance(value, (list, tuple)):
                return tuple(value)
            return tuple(default)

        sensor = ScreenshotSensor(
            helper_argv=helper_argv,
            # gap_minutes/max_minutes were burst-aggregator knobs; the
            # sensor now emits one L1 event per capture so they're no
            # longer wired in. We keep the field defs in DEFAULT_SETTINGS
            # so any existing user YAML doesn't fail validation, but they
            # currently have no effect.
            retention_days=int(settings.get("original_retention_days", DEFAULT_SETTINGS["original_retention_days"])),
            capture_scope=str(settings.get("capture_scope", DEFAULT_SETTINGS["capture_scope"])),
            ocr_languages=_tuple(settings.get("ocr_languages"), DEFAULT_SETTINGS["ocr_languages"]),
            ocr_level=str(settings.get("ocr_level", DEFAULT_SETTINGS["ocr_level"])),
            extra_app_blocklist=_tuple(settings.get("app_blocklist"), DEFAULT_SETTINGS["app_blocklist"]),
            window_title_blocklist=_tuple(settings.get("window_title_blocklist"), DEFAULT_SETTINGS["window_title_blocklist"]),
            thumbnail_max_width=int(settings.get("thumbnail_max_width", DEFAULT_SETTINGS["thumbnail_max_width"])),
            jpeg_quality_original=int(settings.get("jpeg_quality_original", DEFAULT_SETTINGS["jpeg_quality_original"])),
            jpeg_quality_thumbnail=int(settings.get("jpeg_quality_thumbnail", DEFAULT_SETTINGS["jpeg_quality_thumbnail"])),
            active_window_interval_sec=float(settings.get("active_window_interval_sec", DEFAULT_SETTINGS["active_window_interval_sec"])),
            full_screen_interval_min=float(settings.get("full_screen_interval_min", DEFAULT_SETTINGS["full_screen_interval_min"])),
        )
        self._owned_sensors.append(sensor)

        return [
            (
                "timeline.screenshot",
                sensor,
                SensorSpec(
                    sensor_id="timeline.screenshot",
                    display_name="Screenshot Timeline",
                    description="Continuous screen capture + local OCR fed into magi memory.",
                    domain="timeline",
                    surface="timeline",
                    sync_mode=str(settings.get("sync_mode", DEFAULT_SETTINGS["sync_mode"])),
                    polling_mode=getattr(sensor, "polling_mode", "interval"),
                    fields=_fields("sensors.screenshot_timeline"),
                    metadata={
                        "source_type": "screenshot_timeline",
                        "default_settings": dict(DEFAULT_SETTINGS),
                        "activation_flow": _activation_flow("sensors.screenshot_timeline").model_dump(),
                        "settings_ui_blocks": [
                            block.model_dump() for block in _settings_ui_blocks("sensors.screenshot_timeline")
                        ],
                    },
                ),
            )
        ]

    def get_settings_resources(self) -> list[PluginSettingsResourceSpec]:
        return [
            PluginSettingsResourceSpec(
                resource_name="permissions",
                resource_type="channel_status",
                description="Live macOS permission grants required by the screenshot timeline plugin.",
            ),
        ]

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
                    "description": "Required for keyboard triggers and the panic hotkey (optional).",
                    "description_i18n_key": "screenshot_timeline.permissions.accessibility.description",
                    "status": statuses["accessibility"],
                    "required": False,
                    "settings_url": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
                },
            ],
        }

    def get_settings_actions(self) -> list[PluginSettingsActionSpec]:
        return [
            PluginSettingsActionSpec(
                action_id="request_permissions",
                label="System permissions",
                description=(
                    "Check Screen Recording (required) and Accessibility (optional, for "
                    "keyboard triggers and panic hotkey) permissions. macOS will prompt "
                    "if not yet granted."
                ),
                button_label="Check & request",
                presentation="inline",
                surface="extensions",
                contribution_id="timeline",
                requires_enabled=False,
                order=10,
            ),
        ]

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
        """Stop the screenshot sensor and its helper subprocess on unload.

        The host calls this on reload (settings change, disable, upgrade).
        Without it, every reload leaks the previous sensor: its timer
        keeps ticking, its NSWorkspace observer keeps listening, and its
        helper subprocess keeps consuming memory + battery. The visible
        symptom is "I set the interval to 120s and now captures fire
        every 3s" — actually multiple sensor instances stacking up.
        """
        # Snapshot + clear up front so a re-entrant call is a no-op.
        owned = list(self._owned_sensors)
        self._owned_sensors.clear()
        for sensor in owned:
            try:
                await sensor.stop()
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "plugin.sensor_stop_failed sensor=%r",
                    getattr(sensor, "source_type", sensor),
                )


__all__ = ["ScreenshotTimelinePlugin", "DEFAULT_SETTINGS"]
