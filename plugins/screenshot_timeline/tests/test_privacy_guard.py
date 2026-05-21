"""Tests for privacy guard."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_module() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "privacy_guard.py"
    spec = importlib.util.spec_from_file_location("screenshot_timeline_privacy", module_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # required for @dataclass under Python 3.12
    spec.loader.exec_module(mod)
    return mod


def test_default_blocklist_blocks_known_password_apps() -> None:
    mod = _load_module()
    pg = mod.PrivacyGuard(extra_app_blocklist=mod.DEFAULT_APP_BLOCKLIST)
    for bundle in [
        "com.agilebits.onepassword7",
        "com.1password.1password",
        "com.bitwarden.desktop",
        "com.apple.keychainaccess",
        "com.dashlane.macos",
        "com.lastpass.LastPassMacDesktop",
    ]:
        assert pg.is_app_blocked(bundle) is True, bundle


def test_default_blocklist_allows_normal_apps() -> None:
    mod = _load_module()
    pg = mod.PrivacyGuard(extra_app_blocklist=mod.DEFAULT_APP_BLOCKLIST)
    for bundle in [
        "com.apple.Safari",
        "com.google.Chrome",
        "com.microsoft.VSCode",
        "com.figma.Desktop",
    ]:
        assert pg.is_app_blocked(bundle) is False, bundle


def test_empty_blocklist_blocks_nothing() -> None:
    """When no defaults are passed in, nothing is blocked — defaults live in settings now."""
    pg = _load_module().PrivacyGuard()
    assert pg.is_app_blocked("com.1password.1password") is False
    assert pg.is_app_blocked("com.apple.Safari") is False


def test_user_added_blocklist_entries_take_effect() -> None:
    pg_mod = _load_module()
    pg = pg_mod.PrivacyGuard(extra_app_blocklist=["com.acme.SecretApp", "com.example.*"])
    assert pg.is_app_blocked("com.acme.SecretApp") is True
    assert pg.is_app_blocked("com.example.Anything") is True
    assert pg.is_app_blocked("com.apple.Safari") is False


def test_incognito_detection_from_window_title() -> None:
    pg = _load_module().PrivacyGuard()
    assert pg.is_window_incognito(
        app_bundle="com.google.Chrome", window_title="New Incognito Tab - Google Chrome"
    )
    assert pg.is_window_incognito(
        app_bundle="org.mozilla.firefox", window_title="(Private Browsing)"
    )
    assert pg.is_window_incognito(
        app_bundle="com.apple.Safari", window_title="Private Browsing"
    )
    assert not pg.is_window_incognito(
        app_bundle="com.apple.Safari", window_title="Magi project plan - Notion"
    )


def test_window_title_substring_blocklist() -> None:
    pg = _load_module().PrivacyGuard(window_title_blocklist=["banking", "tax return"])
    assert pg.is_window_title_blocked("Chase Banking Dashboard")
    assert pg.is_window_title_blocked("2024 Tax Return - TurboTax")
    assert not pg.is_window_title_blocked("Magi project plan")


def test_panic_pause_state() -> None:
    pg = _load_module().PrivacyGuard()
    assert not pg.is_panic_active(now=100.0)
    pg.engage_panic(duration_seconds=60, now=100.0)
    assert pg.is_panic_active(now=120.0)
    assert pg.is_panic_active(now=159.0)
    assert not pg.is_panic_active(now=161.0)


def test_panic_release_clears_state() -> None:
    pg = _load_module().PrivacyGuard()
    pg.engage_panic(duration_seconds=60, now=100.0)
    pg.release_panic()
    assert not pg.is_panic_active(now=110.0)


def test_should_skip_capture_combines_all_signals() -> None:
    mod = _load_module()
    pg = mod.PrivacyGuard(extra_app_blocklist=mod.DEFAULT_APP_BLOCKLIST)
    # Allowed
    assert pg.should_skip_capture(
        app_bundle="com.apple.Safari",
        window_title="Magi",
        screen_locked=False,
        now=100.0,
    ) is None
    # Blocked app
    assert pg.should_skip_capture(
        app_bundle="com.1password.1password",
        window_title="any",
        screen_locked=False,
        now=100.0,
    ) == "blocked_app"
    # Locked screen
    assert pg.should_skip_capture(
        app_bundle="com.apple.Safari",
        window_title="Magi",
        screen_locked=True,
        now=100.0,
    ) == "locked"
    # Panic
    pg.engage_panic(duration_seconds=60, now=100.0)
    assert pg.should_skip_capture(
        app_bundle="com.apple.Safari",
        window_title="Magi",
        screen_locked=False,
        now=110.0,
    ) == "panic"
