"""Burst aggregation: cluster consecutive captures of the same window into a single L1 event."""
from __future__ import annotations

from dataclasses import dataclass, field

try:
    from .ids import burst_source_item_id, short_window_hash
except ImportError:  # loaded outside a package context (e.g. tests via spec_from_file_location)
    import importlib.util as _imp_util
    from pathlib import Path as _Path

    _ids_path = _Path(__file__).resolve().parent / "ids.py"
    _spec = _imp_util.spec_from_file_location("screenshot_timeline_ids_inner", _ids_path)
    assert _spec is not None and _spec.loader is not None
    _mod = _imp_util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    burst_source_item_id = _mod.burst_source_item_id
    short_window_hash = _mod.short_window_hash

DEFAULT_CONTENT_CHAR_CAP = 8000


@dataclass
class Capture:
    capture_id: str
    captured_at: float
    app_bundle: str
    window_title: str
    url: str | None
    ocr_text: str
    trigger: str
    scope: str
    original_path: str
    thumbnail_path: str
    dimensions: tuple[int, int]
    ocr_confidence_avg: float
    original_expires_at: float


@dataclass
class ClosedBurst:
    source_item_id: str
    idempotency_key: str
    app_bundle: str
    app_name: str
    window_title: str
    url: str | None
    start_unix: float
    end_unix: float
    duration_seconds: float
    capture_count: int
    captures: list[Capture]
    ocr_text_union: str
    trigger_breakdown: dict[str, int]
    representative_capture_id: str
    ocr_confidence_avg: float
    display_id: str = "primary"


@dataclass
class _OpenBurst:
    app_bundle: str
    window_title: str
    url: str | None
    start_unix: float
    last_unix: float
    captures: list[Capture] = field(default_factory=list)
    seen_lines: list[str] = field(default_factory=list)
    seen_line_set: set[str] = field(default_factory=set)
    trigger_counts: dict[str, int] = field(default_factory=lambda: {
        "timer": 0, "window_switch": 0, "keyboard": 0, "manual": 0
    })


@dataclass
class BurstAggregator:
    gap_minutes: int = 5
    max_minutes: int = 30
    retention_days: int = 30
    content_char_cap: int = DEFAULT_CONTENT_CHAR_CAP

    def __post_init__(self) -> None:
        self._open: _OpenBurst | None = None

    def open_burst_count(self) -> int:
        return 1 if self._open is not None else 0

    def ingest(self, payload: dict) -> list[ClosedBurst]:
        """Ingest one capture. Return any bursts closed as a side-effect."""
        captured_at = float(payload["captured_at"])
        cap = Capture(
            capture_id=str(payload["capture_id"]),
            captured_at=captured_at,
            app_bundle=str(payload["app_bundle"]),
            window_title=str(payload.get("window_title") or ""),
            url=(payload.get("url") or None),
            ocr_text=str(payload.get("ocr_text") or ""),
            trigger=str(payload.get("trigger") or "timer"),
            scope=str(payload.get("scope") or "active_window"),
            original_path=str(payload.get("original_path") or ""),
            thumbnail_path=str(payload.get("thumbnail_path") or ""),
            dimensions=tuple(payload.get("dimensions") or (0, 0)),  # type: ignore[arg-type]
            ocr_confidence_avg=float(payload.get("ocr_confidence_avg") or 0.0),
            original_expires_at=captured_at + self.retention_days * 86400.0,
        )

        closed: list[ClosedBurst] = []
        if self._open is None:
            self._open_new(cap)
            return closed

        # Decide whether to close before extending
        should_cut = (
            cap.app_bundle != self._open.app_bundle
            or cap.window_title != self._open.window_title
            or (cap.captured_at - self._open.last_unix) > self.gap_minutes * 60
            or (cap.captured_at - self._open.start_unix) > self.max_minutes * 60
        )
        if should_cut:
            closed.append(self._close_current())
            self._open_new(cap)
        else:
            self._extend(cap)
        return closed

    def flush_all(self, *, now: float) -> list[ClosedBurst]:
        """Close any open burst, regardless of gap. Returns the closed burst(s)."""
        if self._open is None:
            return []
        closed = [self._close_current()]
        return closed

    def _open_new(self, cap: Capture) -> None:
        ob = _OpenBurst(
            app_bundle=cap.app_bundle,
            window_title=cap.window_title,
            url=cap.url,
            start_unix=cap.captured_at,
            last_unix=cap.captured_at,
        )
        self._open = ob
        self._extend(cap)

    def _extend(self, cap: Capture) -> None:
        assert self._open is not None
        self._open.captures.append(cap)
        self._open.last_unix = cap.captured_at
        self._open.trigger_counts[cap.trigger] = self._open.trigger_counts.get(cap.trigger, 0) + 1
        raw = cap.ocr_text or ""
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped not in self._open.seen_line_set:
                self._open.seen_line_set.add(stripped)
                self._open.seen_lines.append(stripped)

    def _close_current(self) -> ClosedBurst:
        assert self._open is not None
        ob = self._open
        self._open = None
        union = self._compose_union(ob)
        avg_confidence = (
            sum(c.ocr_confidence_avg for c in ob.captures) / max(1, len(ob.captures))
        )
        window_hash = short_window_hash(ob.window_title, ob.app_bundle)
        sid = burst_source_item_id(
            start_unix=ob.start_unix,
            app_bundle=ob.app_bundle,
            window_id_hash=window_hash,
        )
        return ClosedBurst(
            source_item_id=sid,
            idempotency_key=sid,
            app_bundle=ob.app_bundle,
            app_name=_app_name_from_bundle(ob.app_bundle),
            window_title=ob.window_title,
            url=ob.url,
            start_unix=ob.start_unix,
            end_unix=ob.last_unix,
            duration_seconds=max(0.0, ob.last_unix - ob.start_unix),
            capture_count=len(ob.captures),
            captures=list(ob.captures),
            ocr_text_union=union,
            trigger_breakdown=dict(ob.trigger_counts),
            representative_capture_id=ob.captures[0].capture_id,
            ocr_confidence_avg=avg_confidence,
        )

    def _compose_union(self, ob: _OpenBurst) -> str:
        parts = [ob.window_title or ""] + ob.seen_lines
        text = "\n".join(parts)
        if len(text) > self.content_char_cap:
            text = text[: self.content_char_cap] + "\n[truncated]"
        return text


def _app_name_from_bundle(bundle: str) -> str:
    if not bundle:
        return ""
    parts = bundle.split(".")
    return parts[-1].capitalize() if parts else bundle


__all__ = ["BurstAggregator", "ClosedBurst", "Capture"]
