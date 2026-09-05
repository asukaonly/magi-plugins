"""Run packages with their declared shared libraries and the SDK, without Magi."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
# Load the provider SDK before plugin source roots can shadow its package name.
try:
    import telegram.ext
except ModuleNotFoundError:
    pass
import types
package = types.ModuleType("magi_telegram_plugin_test")
package.__path__ = [str(ROOT / "plugins" / "telegram")]
sys.modules[package.__name__] = package
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "plugins"))
