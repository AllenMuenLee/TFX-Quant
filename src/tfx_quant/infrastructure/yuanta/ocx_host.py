r"""OcxHost — hosts one Yuanta ActiveX/OCX control on the wx main UI thread.

**Verified working end-to-end this session (2026-08-16)** against the real vendor
`.ocx` files, under a real x32 Python interpreter, including a live network
round-trip (`SetFutOrdConnection` with placeholder credentials against the vendor's
test endpoint correctly returned `OnLogonS` with a real `TLinkStatus`). Full trail in
`docs/adr/0004-broker-session-architecture.md`'s "Execution attempt findings" — three
things that trail corrected versus this module's first draft, all reflected below:

1. **Activate by ProgID, not by raw file path.** `AtlAxCreateControlEx` does accept a
   bare file path, but silently falls back to hosting the system `IWebBrowser2`
   control (treating the path as something to "navigate to") rather than loading the
   named module — passing the registered ProgID string is what actually works, which
   also matches the vendor's own Python compatibility note (`元大API交易PYTHON注意
   事項.docx`), which only ever shows ProgID strings.
2. **COM registration is required, but not admin rights.** The ProgID must be
   registered for `AtlAxCreateControlEx`/`CoCreateInstance` to resolve it — but
   registration under `HKEY_CURRENT_USER\Software\Classes` (see
   `com_registration.py`) works exactly as well as the admin-only
   `HKEY_LOCAL_MACHINE` path `regsvr32` uses, with no elevation needed. Call
   `com_registration.register_all_per_user()` before constructing this class.
3. **Fix the DLL search path before activating.** The OCX's own CRT/MFC dependency
   DLLs (`msvcr90.dll` etc.) live alongside the `.ocx` file, not alongside
   `python.exe` — Windows' default `LoadLibrary` search order only checks the
   directory of the process's main EXE, not the directory of a DLL being loaded via a
   full path, so those dependencies silently fail to resolve otherwise (surfacing as
   a confusing `WinError 1114` on `msvcr90.dll`'s own `DllMain`, not as a missing-file
   error). Pre-loading the `.ocx` once with `LOAD_WITH_ALTERED_SEARCH_PATH` (which
   makes the loader search the target file's own directory, like a normal EXE
   launched from that folder would) resolves this; the module then stays resident for
   the real activation call to reuse.

Also true, unchanged from the first draft: both OCXs are **windowed** ActiveX controls
(MFC `COleControl`-derived — see `infrastructure/yuanta/README.md`), and hosting them
via `AtlAxCreateControlEx` inside a real (if invisible) window is what the vendor's
own Python note documents — a bare `comtypes.client.CreateObject()`
(`CoCreateInstance` with no in-place activation) does work for calling the *trading*
control's methods (verified), but the *quote* control's `SetMktLogon` raised
`E_UNEXPECTED` ("Catastrophic failure") without a window and succeeded once hosted in
one — so this module hosts both uniformly via a window rather than relying on a
control-specific exception to the rule.

Reads the control's method/event signatures from its own embedded type library via
`comtypes.client.GetModule()` at runtime rather than hardcoding generated bindings —
this session found the shipped quote OCX's live type library disagrees with
`元大行情API.pdf` in several places (extra `ReqType`/`SetMap` parameters not in the
PDF at all — see `infrastructure/yuanta/README.md`), so the type library is the more
current, authoritative source and regenerating from it avoids baking in the PDF's
stale signatures.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from types import ModuleType
from typing import Any

import comtypes
import comtypes.automation
import comtypes.client

try:
    import wx
except ImportError:  # pragma: no cover - wx is a real dependency on win32, absent elsewhere
    wx = None

_kernel32 = ctypes.windll.kernel32
_kernel32.LoadLibraryExW.restype = ctypes.c_void_p
_kernel32.LoadLibraryExW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32]
_LOAD_WITH_ALTERED_SEARCH_PATH = 0x00000008

_atl = ctypes.OleDLL("atl.dll")
_atl.AtlAxCreateControlEx.argtypes = [
    wintypes.LPCWSTR,  # lpszName — must be a registered ProgID (see module docstring)
    wintypes.HWND,  # hWnd — parent window
    ctypes.c_void_p,  # pStream
    ctypes.POINTER(ctypes.c_void_p),  # ppUnkContainer (out)
    ctypes.POINTER(ctypes.c_void_p),  # ppUnkControl (out)
    ctypes.c_void_p,  # iidSink — REFIID; always pass a valid pointer, never NULL (crashes)
    ctypes.c_void_p,  # punkSink — fine as NULL, we advise via comtypes afterward
]
_atl.AtlAxCreateControlEx.restype = wintypes.LONG


def _preload_with_altered_search_path(ocx_path: str) -> None:
    """See module docstring point 3. Idempotent — loading an already-resident module
    by the same path just bumps its reference count."""
    handle = _kernel32.LoadLibraryExW(ocx_path, None, _LOAD_WITH_ALTERED_SEARCH_PATH)
    if not handle:
        err = ctypes.GetLastError()
        raise OSError(f"LoadLibraryExW({ocx_path!r}) failed: {ctypes.WinError(err)}")


class OcxHost:
    """Hosts one ActiveX control by ProgID inside a hidden `wx.Frame`.

    `progid` must already be registered — call
    `com_registration.register_all_per_user()` once before constructing this class.
    """

    def __init__(
        self,
        *,
        progid: str,
        ocx_path: str,
        dispatch_interface_name: str,
        events_interface_name: str,
    ) -> None:
        if wx is None:
            raise RuntimeError("wxPython is required to host the Yuanta ActiveX controls")

        _preload_with_altered_search_path(ocx_path)

        self._generated_module: ModuleType = comtypes.client.GetModule(ocx_path)
        dispatch_interface = getattr(self._generated_module, dispatch_interface_name)
        self._events_interface = getattr(self._generated_module, events_interface_name)

        self._frame = wx.Frame(None, size=(0, 0), style=0)
        hwnd = wintypes.HWND(self._frame.GetHandle())
        self._advise_connection: Any = None
        """Holds `comtypes.client.GetEvents()`'s return value — a COM connection
        point advise is torn down as soon as that object is garbage collected, so it
        must be kept alive for as long as events should keep arriving. Losing this
        reference (e.g. by not storing it at all) silently un-advises the connection
        with no error — events just stop arriving, which is easy to mistake for the
        control never firing them at all."""

        p_container = ctypes.c_void_p()
        p_control = ctypes.c_void_p()
        iid_null = comtypes.GUID()  # valid pointer to a zeroed IID — see argtypes note
        hr = _atl.AtlAxCreateControlEx(
            progid,
            hwnd,
            None,
            ctypes.byref(p_container),
            ctypes.byref(p_control),
            ctypes.byref(iid_null),
            None,
        )
        if hr < 0:
            self._frame.Destroy()
            raise OSError(
                f"AtlAxCreateControlEx({progid!r}) failed, HRESULT=0x{hr & 0xFFFFFFFF:08X}"
            )

        # Dispinterfaces (like this control's) aren't directly QueryInterface-able by
        # their nominal IID — go through IDispatch and cast, matching how comtypes'
        # generated bindings are meant to be used.
        unknown = ctypes.cast(p_control, ctypes.POINTER(comtypes.IUnknown))
        dispatch = unknown.QueryInterface(comtypes.automation.IDispatch)
        self.control: Any = ctypes.cast(dispatch, ctypes.POINTER(dispatch_interface))

    def advise(self, sink: object) -> None:
        """Subscribe `sink` (an object with methods matching the events interface,
        e.g. `OnLogonS`, `OnReportQuery`, ...) to the control's outgoing events."""
        self._advise_connection = comtypes.client.GetEvents(
            self.control, sink, interface=self._events_interface
        )

    def close(self) -> None:
        self._advise_connection = None
        self.control = None
        if self._frame is not None:
            self._frame.Destroy()
            self._frame = None
