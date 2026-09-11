"""Windowed ActiveX hosting shared by the Yuanta order and quote controls.

Both vendor controls must be created inside a real window through
``AtlAxCreateControlEx``.  ``comtypes.client.CreateObject`` returns an instance whose
very first method call fails with ``E_UNEXPECTED`` ("Catastrophic failure"), and the
quote control additionally never starts its network session that way.  The vendor's
own ``YuantaQuoteAPI Sample.py`` uses ``atl.AtlAxCreateControlEx`` against a wx window
handle, which is what this module reproduces.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
from typing import Any

_LOAD_WITH_ALTERED_SEARCH_PATH = 0x00000008


def preload_control(ocx_path: Path) -> None:
    """Load the OCX with its own directory on the dependency search path."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    load_library = kernel32.LoadLibraryExW
    load_library.restype = ctypes.c_void_p
    load_library.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32]
    if not load_library(str(ocx_path), None, _LOAD_WITH_ALTERED_SEARCH_PATH):
        error = ctypes.get_last_error()
        raise OSError(error, f"LoadLibraryExW failed for {ocx_path}")


def create_activex_control(progid: str, hwnd: int, dispatch_interface: Any) -> Any:
    """Create ``progid`` inside the window ``hwnd`` and return its dispatch pointer."""
    import comtypes

    atl = ctypes.OleDLL("atl.dll")
    create = atl.AtlAxCreateControlEx
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.HWND,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(comtypes.IUnknown)),
        ctypes.POINTER(ctypes.POINTER(comtypes.IUnknown)),
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    create.restype = wintypes.LONG
    # Typed output pointers own ATL's references and release them on every exit.
    container = ctypes.POINTER(comtypes.IUnknown)()
    raw_control = ctypes.POINTER(comtypes.IUnknown)()
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
    # QueryInterface supplies a separate reference for the returned owner. Casting
    # an owning comtypes pointer creates another destructor without an AddRef.
    return raw_control.QueryInterface(dispatch_interface)


__all__ = ["create_activex_control", "preload_control"]
