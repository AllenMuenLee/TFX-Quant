"""Typed boundary for Yuanta's documented domestic futures order OCX.

The names and field codes in this module come from ``元大BToCAPI格式.pdf``.  The OCX
does not provide a client-order-id field and its ``Oseq_No`` is an identifier, not a
documented monotonic sequence.  Correlation and dedup keys are consequently derived at
this boundary; application/domain code never sees a raw COM callback.
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol

from tfx_quant.domain.fill import Fill
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, Order, OrderKind, TimeInForce
from tfx_quant.domain.order_state_machine import OrderReport, OrderStatus
from tfx_quant.domain.quantity import Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp


class LegacyOrderApiError(RuntimeError):
    """The documented OCX rejected input or returned an undecidable payload."""


class YuantaOrderControl(Protocol):
    """Only the documented OCX methods used by Feature 06."""

    def SetWaitOrdResult(self, flag: int) -> None: ...

    def SendOrderF(self, *args: str) -> str: ...

    def ReportQuery(self, *args: str) -> int: ...

    def DealQuery(self, *args: str) -> int: ...


SymbolResolver = Callable[[Order], str]
EventPublisher = Callable[[object], None]


@dataclass(frozen=True, slots=True)
class ParsedOrderResult:
    request_id: str
    order_sequence_no: str | None
    error_code: str
    error_message: str


def parse_pipe_fields(payload: str) -> dict[str, str]:
    """Parse the ``Tag=value|...`` format documented for query callbacks."""
    fields: dict[str, str] = {}
    for item in payload.split("|"):
        if not item.strip():
            continue
        if "=" not in item:
            raise LegacyOrderApiError("broker query item has no '=' separator")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key or key in fields:
            raise LegacyOrderApiError(f"missing or duplicate broker query tag: {key!r}")
        fields[key] = value.strip()
    return fields


def parse_pipe_rows(payload: str, row_count: int, *, first_tag: str) -> tuple[dict[str, str], ...]:
    """Split a documented query callback containing repeated tag sets."""
    if row_count == 0:
        return ()
    marker = f"{first_tag}="
    starts = [index for index in range(len(payload)) if payload.startswith(marker, index)]
    if len(starts) != row_count:
        raise LegacyOrderApiError(
            f"broker row count mismatch: declared={row_count}, parsed={len(starts)}"
        )
    starts.append(len(payload))
    return tuple(parse_pipe_fields(payload[starts[i] : starts[i + 1]]) for i in range(row_count))


def parse_order_result(request_id: str, result: str) -> ParsedOrderResult:
    """Parse ``流水號|錯誤代碼|錯誤訊息`` from ``OnOrdResult``."""
    parts = result.split("|", 2)
    if len(parts) != 3:
        raise LegacyOrderApiError("OnOrdResult must contain exactly three pipe fields")
    sequence, error_code, error_message = (part.strip() for part in parts)
    return ParsedOrderResult(
        request_id=request_id.strip(),
        order_sequence_no=sequence or None,
        error_code=error_code,
        error_message=error_message,
    )


def order_report_status(fields: Mapping[str, str]) -> OrderStatus:
    """Map the PDF's ``Statusc``/``Ts_Code`` values without optimistic inference."""
    status_code = _required(fields, "Statusc")
    ts_code = _required(fields, "Ts_Code").zfill(2)
    if status_code == "2" or ts_code == "05":
        return OrderStatus.REJECTED
    if ts_code == "06":
        return OrderStatus.FILLED
    if ts_code in {"07", "09"}:
        return OrderStatus.CANCELLED
    if ts_code == "08":
        return OrderStatus.PARTIALLY_FILLED
    if ts_code in {"01", "04", "11"}:
        return OrderStatus.ACKNOWLEDGED
    if ts_code == "00" or status_code == "1":
        return OrderStatus.SUBMITTING
    return OrderStatus.UNKNOWN


def broker_report_key(fields: Mapping[str, str]) -> str:
    """Stable dedup key made solely from documented order-report fields."""
    return _digest(
        "order",
        _required(fields, "Oseq_No"),
        _required(fields, "Ts_Code"),
        _required(fields, "R_Time"),
        fields.get("Deal_Qty", ""),
        fields.get("Kill_Qty", ""),
    )


def broker_fill_key(fields: Mapping[str, str]) -> str:
    """Stable fill key for an API which documents no separate execution number."""
    return _digest(
        "fill",
        _required(fields, "Oseq_No"),
        _required(fields, "Order_No"),
        _required(fields, "D_Time"),
        _required(fields, "Deal_Qty"),
        _required(fields, "A_Prc"),
    )


def make_order_report(
    fields: Mapping[str, str], client_order_id: ClientOrderId, event_sequence: int
) -> OrderReport:
    status = order_report_status(fields)
    return OrderReport(
        client_order_id=client_order_id,
        status=status,
        broker_seq_no=event_sequence,
        at=Timestamp.now(),
        broker_order_no=fields.get("Order_No") or None,
        reject_reason=(fields.get("Err_Msg") or fields.get("Ts_Msg") or None)
        if status in {OrderStatus.REJECTED, OrderStatus.UNKNOWN}
        else None,
    )


def make_fill(
    fields: Mapping[str, str],
    client_order_id: ClientOrderId,
    instrument: Instrument,
    side: Side,
    event_sequence: int,
) -> Fill:
    try:
        quantity = int(_required(fields, "Deal_Qty"))
        price = Decimal(_required(fields, "A_Prc"))
    except (ValueError, InvalidOperation) as exc:
        raise LegacyOrderApiError("invalid fill quantity or price") from exc
    return Fill(
        client_order_id=client_order_id,
        instrument=instrument,
        side=side,
        quantity=Quantity(quantity),
        price=Price(price),
        at=Timestamp.now(),
        broker_fill_no=broker_fill_key(fields),
        broker_seq_no=event_sequence,
    )


class LegacyOrderApiClient:
    """Thin, testable wrapper over a hosted ``YuantaOrd`` control.

    ``symbol_resolver`` is mandatory because the official documents show examples such
    as ``TXFD9`` but do not specify the year/month encoding algorithm.
    """

    def __init__(self, control: YuantaOrderControl, symbol_resolver: SymbolResolver) -> None:
        self._control = control
        self._symbol_resolver = symbol_resolver
        self._event_sequence = itertools.count(1)
        self._orders: dict[ClientOrderId, Order] = {}
        self._request_ids: dict[str, ClientOrderId] = {}
        self._order_sequences: dict[str, ClientOrderId] = {}
        self._broker_order_numbers: dict[ClientOrderId, str] = {}
        self._seen_report_keys: set[str] = set()
        self._seen_fill_keys: set[str] = set()
        self._submitting_client_id: ClientOrderId | None = None
        self._control.SetWaitOrdResult(0)

    def submit_order(self, order: Order, client_order_id: ClientOrderId) -> str:
        self._orders[client_order_id] = order
        self._submitting_client_id = client_order_id
        try:
            request_id = str(
                self._control.SendOrderF(
                    "01",
                    "0",
                    order.account.branch_id,
                    order.account.account_no,
                    order.account.sub_account,
                    "",
                    "B" if order.side is Side.BUY else "S",
                    self._symbol_resolver(order),
                    str(order.price.amount),
                    str(order.quantity.lots),
                    "0" if order.kind is OrderKind.OPEN else "1",
                    "L",
                    _time_in_force(order.time_in_force),
                    "",
                    "",
                )
            )
        finally:
            self._submitting_client_id = None
        if not request_id:
            raise LegacyOrderApiError("SendOrderF returned an empty asynchronous request id")
        self._request_ids[request_id] = client_order_id
        return request_id

    def cancel_order(self, client_order_id: ClientOrderId) -> str:
        order = self._orders.get(client_order_id)
        broker_no = self._broker_order_numbers.get(client_order_id)
        if order is None or not broker_no:
            raise LegacyOrderApiError(
                "cannot cancel before a uniquely correlated broker order number"
            )
        return str(
            self._control.SendOrderF(
                "03",
                "0",
                order.account.branch_id,
                order.account.account_no,
                order.account.sub_account,
                broker_no,
                "B" if order.side is Side.BUY else "S",
                self._symbol_resolver(order),
                str(order.price.amount),
                "",
                "",
                "L",
                _time_in_force(order.time_in_force),
                "",
                "",
            )
        )

    def handle_order_result(self, request_id: str, result: str) -> ParsedOrderResult:
        parsed = parse_order_result(request_id, result)
        client_id = self._request_ids.get(parsed.request_id) or self._submitting_client_id
        if client_id is not None and parsed.order_sequence_no:
            self._order_sequences[parsed.order_sequence_no] = client_id
        return parsed

    def correlate(self, fields: Mapping[str, str]) -> ClientOrderId | None:
        client_id = self._order_sequences.get(fields.get("Oseq_No", ""))
        if client_id is not None:
            broker_no = fields.get("Order_No")
            if broker_no:
                self._broker_order_numbers[client_id] = broker_no
        return client_id

    def parse_order_report(self, fields: Mapping[str, str]) -> OrderReport | None:
        """Correlate and deduplicate one ``OnOrdRptF``/query row.

        ``None`` means either a replay or an uncorrelatable callback.  The caller must
        safe-pause on the latter; it must never guess which local intent owns it.
        """
        key = broker_report_key(fields)
        if key in self._seen_report_keys:
            return None
        client_id = self.correlate(fields)
        if client_id is None:
            return None
        report = make_order_report(fields, client_id, self.next_event_sequence())
        self._seen_report_keys.add(key)
        return report

    def parse_fill(self, fields: Mapping[str, str]) -> Fill | None:
        """Correlate and deduplicate one ``OnOrdMatF``/query row."""
        key = broker_fill_key(fields)
        if key in self._seen_fill_keys:
            return None
        client_id = self.correlate(fields)
        order = self._orders.get(client_id) if client_id is not None else None
        if client_id is None or order is None:
            return None
        fill = make_fill(
            fields,
            client_id,
            order.instrument,
            order.side,
            self.next_event_sequence(),
        )
        self._seen_fill_keys.add(key)
        return fill

    def next_event_sequence(self) -> int:
        """Local ingestion ordering; deliberately not presented as a broker sequence."""
        return next(self._event_sequence)


def _required(fields: Mapping[str, str], key: str) -> str:
    value = fields.get(key, "").strip()
    if not value:
        raise LegacyOrderApiError(f"broker payload missing required field {key}")
    return value


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def _time_in_force(value: TimeInForce) -> str:
    return {TimeInForce.ROD: "R", TimeInForce.FOK: "F", TimeInForce.IOC: "I"}[value]


__all__ = [
    "LegacyOrderApiClient",
    "LegacyOrderApiError",
    "ParsedOrderResult",
    "broker_fill_key",
    "broker_report_key",
    "make_fill",
    "make_order_report",
    "order_report_status",
    "parse_order_result",
    "parse_pipe_fields",
    "parse_pipe_rows",
]
