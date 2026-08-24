"""Creation and event wiring for the installed 32-bit YuantaQuote ActiveX control."""

from __future__ import annotations

from collections.abc import Callable

from tfx_quant.domain.market_data import MarketDataGap, RawMarketEvent
from tfx_quant.infrastructure.yuanta.quote_adapter import YuantaQuoteAdapter

_PROG_ID = "YUANTAQUOTE.YuantaQuoteCtrl.1"


class _EventSink:
    def __init__(self, adapter: YuantaQuoteAdapter) -> None:
        self._adapter = adapter

    def OnMktStatusChange(self, status: int, message: str) -> None:  # noqa: N802
        self._adapter.on_mkt_status_change(status, message)

    def OnRegError(self, symbol: str, update_mode: int, error_code: int) -> None:  # noqa: N802
        self._adapter.on_reg_error(symbol, update_mode, error_code)

    def OnGetMktAll(self, *values: str) -> None:  # noqa: N802
        if len(values) != 19:
            return
        self._adapter.on_get_mkt_all(*values)


class YuantaQuoteComHost:
    """Owns the COM object and event connection for their entire shared lifetime."""

    def __init__(
        self, on_event: Callable[[RawMarketEvent], None], on_gap: Callable[[MarketDataGap], None]
    ) -> None:
        try:
            import comtypes.client
        except ImportError as exc:
            raise RuntimeError("comtypes 1.1.11 is required by the Yuanta quote runtime") from exc
        try:
            control = comtypes.client.CreateObject(_PROG_ID)
            self.adapter = YuantaQuoteAdapter(control, on_event, on_gap)
            self._sink = _EventSink(self.adapter)
            self._connection = comtypes.client.GetEvents(control, self._sink)
            self._control = control
        except Exception as exc:
            raise RuntimeError(
                "Unable to create the YuantaQuote OCX. Run C:\\Yuanta\\QAPI\\install_ytocx.bat "
                "as administrator under the documented 32-bit Python 3.9 runtime."
            ) from exc

    def close(self) -> None:
        self.adapter.stop()
        disconnect = getattr(self._connection, "disconnect", None)
        if callable(disconnect):
            disconnect()


__all__ = ["YuantaQuoteComHost"]
