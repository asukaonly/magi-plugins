"""Privacy guard: blocklists, incognito detection, lockscreen, panic state."""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

DEFAULT_APP_BLOCKLIST: tuple[str, ...] = (
    "com.agilebits.onepassword*",
    "com.1password.1password*",
    "com.bitwarden.desktop",
    "com.apple.keychainaccess",
    "com.lastpass.LastPassMacDesktop",
    "com.dashlane.*",
    "com.apple.SecurityAgent",
)

_INCOGNITO_PATTERNS = (
    re.compile(r"\bincognito\b", re.IGNORECASE),
    re.compile(r"\bprivate browsing\b", re.IGNORECASE),
    re.compile(r"\(private\b", re.IGNORECASE),
    re.compile(r"\b私密浏览\b"),
    re.compile(r"\b无痕\b"),
)


@dataclass
class PrivacyGuard:
    extra_app_blocklist: tuple[str, ...] | list[str] = ()
    window_title_blocklist: tuple[str, ...] | list[str] = field(default_factory=tuple)
    _panic_until: float = 0.0

    def is_app_blocked(self, app_bundle: str) -> bool:
        if not app_bundle:
            return False
        # The blocklist is owned entirely by settings — defaults flow in via
        # ``extra_app_blocklist`` (seeded from ``DEFAULT_APP_BLOCKLIST`` in
        # ``plugin.py``) so the UI shows users what is actually blocked.
        return any(fnmatch.fnmatchcase(app_bundle, p) for p in self.extra_app_blocklist)

    def is_window_incognito(self, *, app_bundle: str, window_title: str) -> bool:
        # Only consider for browser apps to reduce false positives
        if not _looks_like_browser(app_bundle):
            return False
        return any(p.search(window_title or "") for p in _INCOGNITO_PATTERNS)

    def is_window_title_blocked(self, window_title: str) -> bool:
        title_lower = (window_title or "").lower()
        return any(needle.lower() in title_lower for needle in self.window_title_blocklist)

    def engage_panic(self, *, duration_seconds: int, now: float) -> None:
        self._panic_until = now + max(0, duration_seconds)

    def release_panic(self) -> None:
        self._panic_until = 0.0

    def is_panic_active(self, *, now: float) -> bool:
        return now < self._panic_until

    def should_skip_capture(
        self,
        *,
        app_bundle: str,
        window_title: str,
        screen_locked: bool,
        now: float,
    ) -> str | None:
        """Return a reason code if capture should be skipped, otherwise None."""
        if screen_locked:
            return "locked"
        if self.is_panic_active(now=now):
            return "panic"
        if self.is_app_blocked(app_bundle):
            return "blocked_app"
        if self.is_window_incognito(app_bundle=app_bundle, window_title=window_title):
            return "incognito"
        if self.is_window_title_blocked(window_title):
            return "blocked_title"
        return None


_BROWSER_BUNDLE_HINTS = (
    "safari",
    "chrome",
    "chromium",
    "firefox",
    "edge",
    "brave",
    "arc",
    "opera",
    "vivaldi",
)


def _looks_like_browser(app_bundle: str) -> bool:
    bundle_lower = (app_bundle or "").lower()
    return any(h in bundle_lower for h in _BROWSER_BUNDLE_HINTS)


__all__ = ["PrivacyGuard", "DEFAULT_APP_BLOCKLIST"]
