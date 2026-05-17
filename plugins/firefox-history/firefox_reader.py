"""Firefox history reader wrapper."""
from __future__ import annotations

import sys
from pathlib import Path

_CORE_PARENT = Path(__file__).resolve().parents[1]
if str(_CORE_PARENT) not in sys.path:
    sys.path.append(str(_CORE_PARENT))

from browser_history_core.firefox_reader import FirefoxHistoryReader, _default_firefox_root

__all__ = ["FirefoxHistoryReader", "_default_firefox_root"]
