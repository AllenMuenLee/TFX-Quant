"""Windowed entry point for the installed build."""
import os
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))

# A named mutex the installer/uninstaller detect (installer.iss AppMutex) so an
# upgrade can ask the operator to close a running instance before files change.
if sys.platform == "win32":
    import ctypes

    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "tfx_quant_desktop_singleton")

_config = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    / "tfx_quant"
    / "config"
    / "settings.json"
)
sys.argv = ["tfx-quant-desktop", *([str(_config)] if _config.is_file() else [])]
runpy.run_module("tfx_quant.desktop", run_name="__main__")
