from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType


PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "chatgpt_history_under_test"

package = ModuleType(PACKAGE_NAME)
package.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
sys.modules.setdefault(PACKAGE_NAME, package)
