from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from decimal import Decimal

from tfx_quant.domain.market_data import (
    MarketDataGap,
    MarketEventQuality,
    RawMarketEvent,
    RecordedMarketEvent,
)
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp

_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_events (
 session_id TEXT NOT NULL, sequence INTEGER NOT NULL, symbol TEXT NOT NULL,
 raw_fields TEXT NOT NULL, match_time_raw TEXT NOT NULL, matched_at_utc TEXT,
 matched_at_taipei TEXT, received_at_utc TEXT NOT NULL, received_at_taipei TEXT NOT NULL,
 match_price TEXT, match_quantity INTEGER, total_match_quantity INTEGER,
 quality TEXT NOT NULL, rejection_reason TEXT, PRIMARY KEY(session_id, sequence));
CREATE INDEX IF NOT EXISTS idx_market_events_symbol_time
 ON market_events(symbol, matched_at_taipei, session_id, sequence);
CREATE TABLE IF NOT EXISTS market_data_gaps (
 id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, start_at TEXT NOT NULL,
 end_at TEXT, reason TEXT NOT NULL, UNIQUE(symbol, start_at, reason));
"""


class SqliteMarketEventRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def append(self, event: RecordedMarketEvent) -> bool:
        raw = event.raw
        local_match = event.matched_at_taipei
        received_local = raw.received_at.value
        values = (
            raw.session_id,
            raw.sequence,
            raw.symbol,
            json.dumps(dict(raw.fields), ensure_ascii=False, sort_keys=True),
            event.match_time_raw,
            None if local_match is None else local_match.astimezone(UTC).isoformat(),
            None if local_match is None else local_match.isoformat(),
            received_local.astimezone(UTC).isoformat(),
            received_local.isoformat(),
            None if event.match_price is None else str(event.match_price),
            event.match_quantity,
            event.total_match_quantity,
            event.quality.value,
            event.rejection_reason,
        )
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO market_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def list_events(self, symbol: str) -> list[RecordedMarketEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id,sequence,symbol,raw_fields,match_time_raw,matched_at_taipei,"
                "received_at_taipei,match_price,match_quantity,total_match_quantity,quality,"
                "rejection_reason FROM market_events WHERE symbol=? "
                "ORDER BY received_at_taipei,session_id,sequence",
                (symbol,),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def record_gap(self, gap: MarketDataGap) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO market_data_gaps(symbol,start_at,end_at,reason) "
                "VALUES(?,?,?,?)",
                (
                    gap.symbol,
                    gap.start.value.isoformat(),
                    None if gap.end is None else gap.end.value.isoformat(),
                    gap.reason,
                ),
            )
            self._conn.commit()

    def list_gaps(self, symbol: str) -> list[MarketDataGap]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT start_at,end_at,reason FROM market_data_gaps "
                "WHERE symbol=? ORDER BY start_at",
                (symbol,),
            ).fetchall()
        return [
            MarketDataGap(
                symbol,
                Timestamp(datetime.fromisoformat(a)),
                None if b is None else Timestamp(datetime.fromisoformat(b)),
                r,
            )
            for a, b, r in rows
        ]


def _event_from_row(row: tuple[object, ...]) -> RecordedMarketEvent:
    (
        session,
        seq,
        symbol,
        fields,
        match_raw,
        matched,
        received,
        price,
        qty,
        total,
        quality,
        reason,
    ) = row
    raw = RawMarketEvent(
        str(symbol),
        int(str(seq)),
        str(session),
        Timestamp(datetime.fromisoformat(str(received))),
        json.loads(str(fields)),
    )
    local = None if matched is None else datetime.fromisoformat(str(matched)).astimezone(TAIPEI_TZ)
    return RecordedMarketEvent(
        raw,
        MarketEventQuality(str(quality)),
        str(match_raw),
        None if local is None else Timestamp(local),
        local,
        None if price is None else Decimal(str(price)),
        None if qty is None else int(str(qty)),
        None if total is None else int(str(total)),
        None if reason is None else str(reason),
    )


__all__ = ["SqliteMarketEventRepository"]
