"""Creation and event wiring for the installed 32-bit YuantaQuote ActiveX control.

Mirrors ``YuantaQuoteAPI Sample.py``: the control is created with
``AtlAxCreateControlEx`` against a real window handle.  The vendor sample declares a
COM ``this`` pointer first, but comtypes removes it before invoking a sink wired with
an explicit event interface; the feed's ``ReqType`` remains the last argument.
"""

from __future__ import annotations

import os
import struct
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from tfx_quant.application.ports.quote_gateway import (
    QuoteConnectionState,
    QuoteRequestType,
    QuoteUpdateMode,
)
from tfx_quant.domain.market_data import MarketDataGap, RawMarketEvent
from tfx_quant.infrastructure.yuanta.ocx_hosting import create_activex_control, preload_control
from tfx_quant.infrastructure.yuanta.quote_adapter import YuantaQuoteAdapter

_PROG_ID = "YUANTAQUOTE.YuantaQuoteCtrl.1"
_OCX_NAME = "YuantaQuote_v2.1.2.9.ocx"


def default_quote_api_directory() -> Path:
    """Where the vendor tells operators to place the QAPI folder."""
    configured = os.environ.get("TFX_QUANT_YUANTA_QAPI_DIR")
    if configured:
        return Path(configured)
    installed = Path("C:/Yuanta/QAPI")
    if installed.is_dir():
        return installed
    root = Path(__file__).resolve().parents[4]
    return root / "行情API元件及說明文件" / "行情API元件及說明文件" / "QAPI"


class _EventSink:
    """Receives every documented event with the arity the control actually sends.

    The vendor sample's handlers include ``this`` because they let ``GetEvents`` infer
    the interface.  This host supplies ``_DYuantaQuoteEvents`` explicitly, whose
    comtypes ``without_this`` wrapper strips that pointer before calling these methods.
    ``ReqType`` is still echoed as the final callback argument.
    """

    def __init__(self, adapter: YuantaQuoteAdapter) -> None:
        self._adapter = adapter

    def OnMktStatusChange(self, status: int, msg: str, req_type: int) -> None:  # noqa: N802
        self._adapter.on_mkt_status_change(status, msg, req_type)

    def OnRegError(  # noqa: N802
        self, symbol: str, update_mode: int, error_code: int, req_type: int
    ) -> None:
        self._adapter.on_reg_error(symbol, update_mode, error_code, req_type)

    def OnGetMktAll(self, *values: object) -> None:  # noqa: N802
        self._adapter.on_get_mkt_all(*values)  # type: ignore[arg-type]


class YuantaQuoteComHost:
    """Owns the COM object and event connection for their entire shared lifetime."""

    def __init__(
        self,
        on_event: Callable[[RawMarketEvent], None],
        on_gap: Callable[[MarketDataGap], None],
        *,
        api_directory: Path | None = None,
        parent: Any | None = None,
    ) -> None:
        if sys.platform != "win32":
            raise RuntimeError("YuantaQuote ActiveX hosting requires Windows")
        if struct.calcsize("P") * 8 != 32:
            raise RuntimeError(
                "The documented YuantaQuote runtime is 32-bit; run the 32-bit interpreter"
            )
        directory = api_directory or default_quote_api_directory()
        ocx_path = directory / _OCX_NAME
        if not ocx_path.is_file():
            raise RuntimeError(f"Yuanta quote ActiveX control not found: {ocx_path}")

        try:
            import comtypes.client
        except ImportError as exc:
            raise RuntimeError("comtypes is required by the Yuanta quote runtime") from exc
        import wx

        if not wx.IsMainThread():
            raise RuntimeError("YuantaQuote ActiveX control must be created on the wx UI thread")
        app = wx.GetApp()
        owner = parent if parent is not None else (app.GetTopWindow() if app is not None else None)
        if owner is None:
            raise RuntimeError(
                "YuantaQuote ActiveX control requires an initialized wx top-level window"
            )

        self._thread_id = threading.get_ident()
        # A real container window: the control is inert when created with CreateObject.
        self._frame: Any = wx.Frame(owner, size=(0, 0), style=0)
        try:
            preload_control(ocx_path)
            generated = comtypes.client.GetModule(str(ocx_path))
            control = create_activex_control(
                _PROG_ID, self._frame.GetHandle(), generated._DYuantaQuote
            )
            self.adapter = YuantaQuoteAdapter(control, on_event, on_gap)
            self._sink = _EventSink(self.adapter)
            # The connection object owns the COM advise cookie and must stay alive.
            self._connection = comtypes.client.GetEvents(
                control, self._sink, interface=generated._DYuantaQuoteEvents
            )
            self._control = control
        except BaseException as exc:
            self._frame.Destroy()
            self._frame = None
            raise RuntimeError(
                f"Unable to create the YuantaQuote OCX from {ocx_path}. Run "
                f"{directory / 'install_ytocx.bat'} as administrator under the documented "
                "32-bit Python runtime."
            ) from exc

    @property
    def state(self) -> QuoteConnectionState:
        return self.adapter.state

    def connect(
        self,
        user_id: str,
        password: SecretStr,
        host: str,
        port: int,
        request_type: QuoteRequestType,
    ) -> None:
        self._assert_ui_thread()
        self.adapter.connect(user_id, password, host, port, request_type)

    def subscribe(
        self,
        symbol: str,
        request_type: QuoteRequestType,
        mode: QuoteUpdateMode = QuoteUpdateMode.SNAPSHOT_UPDATE,
    ) -> None:
        self._assert_ui_thread()
        self.adapter.subscribe(symbol, request_type, mode)

    def unsubscribe(self, symbol: str, request_type: QuoteRequestType) -> None:
        self._assert_ui_thread()
        self.adapter.unsubscribe(symbol, request_type)

    def stop(self) -> None:
        self.close()

    def close(self) -> None:
        self._assert_ui_thread()
        if self._frame is None:
            return
        try:
            self.adapter.stop()
        finally:
            disconnect = getattr(self._connection, "disconnect", None)
            if callable(disconnect):
                disconnect()
            self._connection = None
            self._frame.Destroy()
            self._frame = None

    def _assert_ui_thread(self) -> None:
        if threading.get_ident() != self._thread_id:
            raise RuntimeError(
                "YuantaQuote ActiveX access must remain on its creating wx UI thread"
            )


__all__ = ["YuantaQuoteComHost", "default_quote_api_directory"]
