"""ATL/comtypes host for the documented ``YuantaOrd`` ActiveX control."""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any

_EVENT_NAMES = frozenset(
    {
        "OnLogonS",
        "OnOrdResult",
        "OnOrdRptF",
        "OnOrdMatF",
        "OnReportQuery",
        "OnDealQuery",
        "OnUserDefinsFuncResult",
    }
)
_LOAD_WITH_ALTERED_SEARCH_PATH = 0x00000008


def default_api_directory() -> Path:
    configured = os.environ.get("TFX_QUANT_YUANTA_API_DIR")
    if configured:
        return Path(configured)
    folder = "API_x64" if struct.calcsize("P") * 8 == 64 else "API"
    installed = Path("C:/Yuanta") / folder
    if installed.is_dir():
        return installed
    return Path(__file__).resolve().parents[4] / "交易API元件及說明文件" / folder


def yuanta_control_progid() -> str:
    """Return the ProgID documented by Yuanta for this Python bitness."""
    return "Yuanta.YuantaOrdCtrl.64" if struct.calcsize("P") * 8 == 64 else "Yuanta.YuantaOrdCtrl.1"


def is_control_registered() -> bool:
    """Check whether the vendor installer registered the matching OCX bitness."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{yuanta_control_progid()}\CLSID"):
            return True
    except FileNotFoundError:
        return False


def _preload_control(ocx_path: Path) -> None:
    """Load the OCX with its directory in the dependency search path."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    load_library = kernel32.LoadLibraryExW
    load_library.restype = ctypes.c_void_p
    load_library.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32]
    if not load_library(str(ocx_path), None, _LOAD_WITH_ALTERED_SEARCH_PATH):
        error = ctypes.get_last_error()
        raise OSError(error, f"LoadLibraryExW failed for {ocx_path}")


class YuantaOcxHost:
    """Host one Yuanta OCX in a real ATL window on the wx UI thread."""

    def __init__(self, *, api_directory: Path | None = None, parent: Any | None = None) -> None:
        if sys.platform != "win32":
            raise RuntimeError("YuantaOrd ActiveX hosting requires Windows")

        directory = api_directory or default_api_directory()
        is_64_bit = struct.calcsize("P") * 8 == 64
        ocx_path = directory / ("YuantaOrd64.ocx" if is_64_bit else "YuantaOrd.ocx")
        if not ocx_path.is_file():
            raise RuntimeError(f"Yuanta order ActiveX control not found: {ocx_path}")
        if not is_control_registered():
            installer = "install_YTFutOrdAP64.bat" if is_64_bit else "install_YTFutOrdAP.bat"
            raise RuntimeError(
                f"{yuanta_control_progid()} is not registered; run {directory / installer} "
                "as Administrator, then restart the application"
            )

        import comtypes
        import comtypes.automation
        import comtypes.client
        import wx

        if not wx.IsMainThread():
            raise RuntimeError("YuantaOrd ActiveX control must be created on the wx UI thread")
        app = wx.GetApp()
        owner = parent if parent is not None else (app.GetTopWindow() if app is not None else None)
        if owner is None:
            raise RuntimeError(
                "YuantaOrd ActiveX control requires an initialized wx top-level window"
            )

        self._thread_id = threading.get_ident()
        self._handlers: dict[str, Callable[..., object]] = {}
        self._advise_connection: Any = None
        self.control: Any = None
        self._frame: Any = wx.Frame(owner, size=(0, 0), style=0)
        try:
            _preload_control(ocx_path)
            generated = comtypes.client.GetModule(str(ocx_path))
            dispatch_interface = generated._DYuantaOrd
            events_interface = generated._DYuantaOrdEvents
            self.control = self._create_control(
                yuanta_control_progid(), self._frame.GetHandle(), dispatch_interface
            )
            # The connection object owns the COM advise cookie and must stay alive.
            self._advise_connection = comtypes.client.GetEvents(
                self.control, self, interface=events_interface
            )
        except BaseException:
            self._frame.Destroy()
            self._frame = None
            raise

    @staticmethod
    def _create_control(progid: str, hwnd: int, dispatch_interface: Any) -> Any:
        import comtypes
        import comtypes.automation

        atl = ctypes.OleDLL("atl.dll")
        create = atl.AtlAxCreateControlEx
        create.argtypes = [
            wintypes.LPCWSTR,
            wintypes.HWND,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        create.restype = wintypes.LONG
        container = ctypes.c_void_p()
        raw_control = ctypes.c_void_p()
        null_iid = comtypes.GUID()
        result = create(
            progid,
            wintypes.HWND(hwnd),
            None,
            ctypes.byref(container),
            ctypes.byref(raw_control),
            ctypes.byref(null_iid),
            None,
        )
        if result < 0:
            raise OSError(
                f"AtlAxCreateControlEx({progid!r}) failed, HRESULT=0x{result & 0xFFFFFFFF:08X}"
            )
        unknown = ctypes.cast(raw_control, ctypes.POINTER(comtypes.IUnknown))
        dispatch = unknown.QueryInterface(comtypes.automation.IDispatch)
        return ctypes.cast(dispatch, ctypes.POINTER(dispatch_interface))

    def bind(self, event_name: str, handler: Callable[..., object]) -> None:
        """Route one documented COM callback to an application handler."""
        self._assert_ui_thread()
        if event_name not in _EVENT_NAMES:
            raise ValueError(f"unsupported YuantaOrd event: {event_name}")
        if event_name in self._handlers:
            raise ValueError(f"YuantaOrd event already bound: {event_name}")
        self._handlers[event_name] = handler

    def close(self) -> None:
        self._assert_ui_thread()
        if self.control is None:
            return
        try:
            self.control.DoLogout()
        finally:
            self._handlers.clear()
            self._advise_connection = None
            self.control = None
            self._frame.Destroy()
            self._frame = None

    def _dispatch(self, event_name: str, *args: object) -> object | None:
        handler = self._handlers.get(event_name)
        return handler(*args) if handler is not None else None

    def _assert_ui_thread(self) -> None:
        if threading.get_ident() != self._thread_id:
            raise RuntimeError("YuantaOrd ActiveX access must remain on its creating wx UI thread")

    def OnLogonS(self, *args: object) -> object | None:  # noqa: N802
        return self._dispatch("OnLogonS", *args)

    def OnOrdResult(self, *args: object) -> object | None:  # noqa: N802
        return self._dispatch("OnOrdResult", *args)

    def OnOrdRptF(self, *args: object) -> object | None:  # noqa: N802
        return self._dispatch("OnOrdRptF", *args)

    def OnOrdMatF(self, *args: object) -> object | None:  # noqa: N802
        return self._dispatch("OnOrdMatF", *args)

    def OnReportQuery(self, *args: object) -> object | None:  # noqa: N802
        return self._dispatch("OnReportQuery", *args)

    def OnDealQuery(self, *args: object) -> object | None:  # noqa: N802
        return self._dispatch("OnDealQuery", *args)

    def OnUserDefinsFuncResult(self, *args: object) -> object | None:  # noqa: N802
        return self._dispatch("OnUserDefinsFuncResult", *args)


__all__ = [
    "YuantaOcxHost",
    "default_api_directory",
    "is_control_registered",
    "yuanta_control_progid",
]
