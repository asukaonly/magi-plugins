"""Screenshot Timeline plugin entry point."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from magi_plugin_sdk import (
    ActivationFlowSpec,
    ExtensionFieldOption,
    ExtensionFieldSpec,
    Plugin,
)
from magi_plugin_sdk.sensors import SensorSpec

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
    "app_blocklist": [],
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
            "Continuously captures your screen, runs local OCR via Apple Vision, "
            "and feeds the results into magi memory. Screenshots stay on this Mac. "
            "Screen Recording permission is required; Accessibility permission is "
            "optional (only needed for keyboard triggers and panic hotkey)."
        ),
        confirm_label="I understand — enable",
        cancel_label="Not now",
        enabled_key=f"{prefix}.enabled",
        configured_key=f"{prefix}.initial_sync_configured",
        fields=[
            ExtensionFieldSpec(
                key=f"{prefix}.capture_scope",
                type="select",
                label="Capture scope",
                description="What part of the screen to capture each tick.",
                default="hybrid",
                options=[
                    ExtensionFieldOption(label="Active window + periodic full screen", value="hybrid"),
                    ExtensionFieldOption(label="Active window only", value="active_window"),
                    ExtensionFieldOption(label="Primary display (full screen)", value="full_screen"),
                ],
                section="activation",
                surface="timeline",
                order=10,
            ),
            ExtensionFieldSpec(
                key=f"{prefix}.original_retention_days",
                type="number",
                label="Original retention (days)",
                description="Originals older than this are deleted; thumbnails are kept permanently.",
                default=30,
                section="activation",
                surface="timeline",
                order=20,
            ),
        ],
    )


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
            description="Accurate is slower but better quality; fast is the opposite.",
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
            description="Bundle IDs to never capture. Glob patterns supported (e.g. com.example.*).",
            default=[],
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

    def get_sensors(self) -> list[tuple[str, Any, SensorSpec]]:
        settings: dict[str, Any] = {}
        sensors_settings = self.settings.get("sensors", {})
        if isinstance(sensors_settings, dict):
            settings = dict(sensors_settings.get("timeline", {}))

        plugin_dir = Path(__file__).resolve().parent
        helper_argv = [str(plugin_dir / "bin" / "magi-vision-helper")]

        def _tuple(value: Any, default: Any) -> tuple:
            if isinstance(value, (list, tuple)):
                return tuple(value)
            return tuple(default)

        sensor = ScreenshotSensor(
            helper_argv=helper_argv,
            gap_minutes=int(settings.get("gap_minutes", DEFAULT_SETTINGS["gap_minutes"])),
            max_minutes=int(settings.get("max_minutes", DEFAULT_SETTINGS["max_minutes"])),
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
                    fields=_fields("sensors.timeline"),
                    metadata={
                        "source_type": "screenshot_timeline",
                        "default_settings": dict(DEFAULT_SETTINGS),
                        "activation_flow": _activation_flow("sensors.timeline").model_dump(),
                    },
                ),
            )
        ]


__all__ = ["ScreenshotTimelinePlugin", "DEFAULT_SETTINGS"]
