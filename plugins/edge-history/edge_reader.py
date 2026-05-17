"""Edge history reader built on Chromium schema."""
from __future__ import annotations

import sys
from pathlib import Path

_CORE_PARENT = Path(__file__).resolve().parents[1]
if str(_CORE_PARENT) not in sys.path:
    sys.path.append(str(_CORE_PARENT))

from browser_history_core.chromium_reader import ChromiumHistoryReader

DEFAULT_WINDOWS_EDGE_ROOT = "~/AppData/Local/Microsoft/Edge/User Data"


def _default_edge_root() -> str:
    return DEFAULT_WINDOWS_EDGE_ROOT


class EdgeHistoryReader(ChromiumHistoryReader):
    """Read and normalize Microsoft Edge history visits."""

    def __init__(self) -> None:
        super().__init__(default_root=_default_edge_root(), browser_label="Edge")
