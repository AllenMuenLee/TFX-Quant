"""連線健康與安全暫停 — per-channel connectivity health tracking and the safe-pause
audit record.

Never a single collapsed "connected" boolean: `application/ports/broker_session.py`'s
`SessionCapabilities` already established the "keep independent booleans" precedent for
login/market_data/trading/order_reports/queries; this module tracks the same five
channels, plus each one's own last-message time, heartbeat, latency, and error — none of
which `SessionCapabilities` carries. See
`docs/adr/0011-connectivity-reconnect-and-safe-pause.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tfx_quant.domain.strategy_state import StrategyState
from tfx_quant.domain.timestamp import Timestamp


class ChannelId(StrEnum):
    """Mirrors `application.ports.broker_session.SessionCapabilities`'s five
    independent flags, plus tracks each one's own timing/latency/error. The Yuanta
    order and quote sessions make several of these co-vary in practice, but they stay independently
    modeled here per the "不以單一布林值代表所有連線" requirement, so a future
    vendor-confirmed independent failure mode (e.g. a genuinely separate market-data
    feed outage) needs no redesign."""

    LOGIN = "LOGIN"
    MARKET_DATA = "MARKET_DATA"
    TRADE = "TRADE"
    ORDER_REPORTS = "ORDER_REPORTS"
    QUERIES = "QUERIES"


class SafePauseReason(StrEnum):
    """The implementation prompt's exact trigger list: "行情 stale、回報中斷、交易通道
    失效、查詢失敗或時鐘大幅偏移時進入 PausedSafe"."""

    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    ORDER_REPORTS_INTERRUPTED = "ORDER_REPORTS_INTERRUPTED"
    TRADE_CHANNEL_INVALID = "TRADE_CHANNEL_INVALID"
    QUERY_FAILED = "QUERY_FAILED"
    CLOCK_SKEW = "CLOCK_SKEW"


@dataclass(frozen=True, slots=True)
class ChannelHealth:
    """One channel's independently-tracked health snapshot."""

    channel: ChannelId
    connected: bool
    last_message_at: Timestamp | None
    last_heartbeat_at: Timestamp | None
    latency_ms: float | None
    last_error: str | None
    is_stale: bool

    @property
    def is_healthy(self) -> bool:
        return self.connected and not self.is_stale and self.last_error is None

    @classmethod
    def initial(cls, channel: ChannelId) -> ChannelHealth:
        """Before any signal has ever been observed — deliberately `connected=False`/
        `is_stale=True` rather than an optimistic default, matching
        `application.market_data.bar_service.MarketDataBarService`'s own
        `_ActiveContract.is_stale: bool = True` default."""
        return cls(
            channel=channel,
            connected=False,
            last_message_at=None,
            last_heartbeat_at=None,
            latency_ms=None,
            last_error=None,
            is_stale=True,
        )


def clock_skew_seconds(local_at: Timestamp, remote_at: Timestamp) -> float:
    """Absolute difference between this process's clock and a broker-stamped
    timestamp (`domain.order_state_machine.OrderReport.at` /
    `domain.fill.Fill.at` / `domain.position.Position.as_of`) — see
    `docs/adr/0011-connectivity-reconnect-and-safe-pause.md`'s discussion of why these
    already-flowing fields, not a dedicated vendor time-sync call, are the basis for
    clock-skew detection."""
    return abs((local_at.value - remote_at.value).total_seconds())


@dataclass(frozen=True, slots=True)
class SafePauseRecord:
    """The audit record for one connectivity safe-pause episode — "第一個觸發原因、
    偵測時間、暫停生效時間、當時策略／委託／持倉摘要及被阻擋的新意圖" as
    independently-inspectable fields, same "never collapse to free text" convention as
    `domain.position_reconciliation.ReconciliationRecord`. Only ever built for the
    *first* trigger of a pause episode — see `application.connectivity.
    connectivity_monitor.ConnectivityMonitor`'s "only escalate from RUNNING" gate,
    mirroring `docs/adr/0010-position-reconciliation-and-manual-sync.md` decision 2."""

    correlation_id: str
    reason: SafePauseReason
    channel: ChannelId
    detail: str
    detected_at: Timestamp
    effective_at: Timestamp
    strategy_state_before: StrategyState
    resulting_strategy_state: StrategyState | None
    active_order_count: int
    expected_net_lots: int | None
    blocked_intent_count: int
    reconciled: bool = False
    """Set once a fresh `BrokerSessionReady` has been observed following this episode
    and the synchronous reconnect-reconciliation fan-out (order/fill/position) has run
    — see `ConnectivityMonitor`'s subscription-order note. Never means "safe to
    resume": resuming stays a human action through `Starting`'s full safety checklist,
    same as every other feature's pause."""


__all__ = [
    "ChannelHealth",
    "ChannelId",
    "SafePauseReason",
    "SafePauseRecord",
    "clock_skew_seconds",
]
