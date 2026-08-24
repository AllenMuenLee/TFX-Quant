from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest

from tfx_quant.application.ports.bar_record_repository import (
    BarRecordNotFoundError,
    BarUpsertOutcome,
)
from tfx_quant.domain.bar import Bar
from tfx_quant.domain.bar_record import (
    BarConflictAudit,
    BarDataSource,
    BarPeriod,
    BarRecord,
    MarketSession,
)
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.persistence.sqlite_bar_record_repository import SqliteBarRecordRepository

_INSTRUMENT = Instrument.TXF
_CONTRACT = ContractMonth(year=2026, month=9)
_OTHER_CONTRACT = ContractMonth(year=2026, month=10)


def _ts(y: int, m: int, d: int, hh: int, mm: int) -> Timestamp:
    return Timestamp(datetime(y, m, d, hh, mm, tzinfo=TAIPEI_TZ))


def _bar(
    start: Timestamp, end: Timestamp, *, price: str = "17500", contract: ContractMonth = _CONTRACT
) -> Bar:
    return Bar(
        instrument=_INSTRUMENT,
        contract=contract,
        open=Price(Decimal(price)),
        high=Price(Decimal(price)),
        low=Price(Decimal(price)),
        close=Price(Decimal(price)),
        volume=3,
        start=start,
        end=end,
    )


def _record(
    start: Timestamp,
    end: Timestamp,
    *,
    price: str = "17500",
    contract: ContractMonth = _CONTRACT,
    is_gap_recovery: bool = False,
) -> BarRecord:
    return BarRecord(
        bar=_bar(start, end, price=price, contract=contract),
        period=BarPeriod.SIXTY_MINUTE,
        trading_day=start.value.date(),
        session=MarketSession.DAY,
        source=BarDataSource.POLLED_FROM_YFINANCE,
        is_gap_recovery=is_gap_recovery,
        created_at=start,
        updated_at=start,
    )


@pytest.fixture
def repo() -> SqliteBarRecordRepository:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    return SqliteBarRecordRepository(connection)


def test_upsert_new_bar_is_inserted(repo: SqliteBarRecordRepository) -> None:
    record = _record(_ts(2026, 9, 16, 8, 45), _ts(2026, 9, 16, 9, 45))
    outcome = repo.upsert_closed_bar(record)
    assert outcome is BarUpsertOutcome.INSERTED

    found = repo.list_recent(
        _INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE, since_trading_day=date(2026, 1, 1)
    )
    assert len(found) == 1
    assert found[0].bar.open.amount == Decimal("17500")
    assert found[0].bar.start == record.bar.start
    assert found[0].session is MarketSession.DAY
    assert found[0].source is BarDataSource.POLLED_FROM_YFINANCE


def test_upsert_identical_duplicate_is_ignored(repo: SqliteBarRecordRepository) -> None:
    record = _record(_ts(2026, 9, 16, 8, 45), _ts(2026, 9, 16, 9, 45))
    assert repo.upsert_closed_bar(record) is BarUpsertOutcome.INSERTED
    assert repo.upsert_closed_bar(record) is BarUpsertOutcome.DUPLICATE_IGNORED

    found = repo.list_recent(
        _INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE, since_trading_day=date(2026, 1, 1)
    )
    assert len(found) == 1


def test_upsert_conflicting_ohlcv_is_rejected_and_original_kept(
    repo: SqliteBarRecordRepository,
) -> None:
    start, end = _ts(2026, 9, 16, 8, 45), _ts(2026, 9, 16, 9, 45)
    original = _record(start, end, price="17500")
    conflicting = _record(start, end, price="17999")
    assert repo.upsert_closed_bar(original) is BarUpsertOutcome.INSERTED
    assert repo.upsert_closed_bar(conflicting) is BarUpsertOutcome.CONFLICT_REJECTED

    found = repo.list_recent(
        _INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE, since_trading_day=date(2026, 1, 1)
    )
    assert len(found) == 1
    assert found[0].bar.open.amount == Decimal("17500")  # never silently overwritten


def test_apply_correction_bumps_revision_and_writes_audit(repo: SqliteBarRecordRepository) -> None:
    start, end = _ts(2026, 9, 16, 8, 45), _ts(2026, 9, 16, 9, 45)
    original = _record(start, end, price="17500")
    repo.upsert_closed_bar(original)

    corrected = BarRecord(
        bar=_bar(start, end, price="17650"),
        period=BarPeriod.SIXTY_MINUTE,
        trading_day=start.value.date(),
        session=MarketSession.DAY,
        source=BarDataSource.POLLED_FROM_YFINANCE,
        is_gap_recovery=False,
        created_at=start,
        updated_at=_ts(2026, 9, 16, 10, 0),
    )
    repo.apply_correction(corrected, reason="修復程序：重新聚合後 close 值更正")

    found = repo.list_recent(
        _INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE, since_trading_day=date(2026, 1, 1)
    )
    assert len(found) == 1
    assert found[0].bar.close.amount == Decimal("17650")
    assert found[0].revision == 2
    assert found[0].updated_at == _ts(2026, 9, 16, 10, 0)

    # White-box check of the audit trail — no public query method exists for it yet
    # (nothing currently reads it back besides an operator inspecting the DB directly).
    revision_rows = repo._conn.execute(
        "SELECT reason, previous_close, new_close FROM bar_record_revisions"
    ).fetchall()
    assert revision_rows == [("修復程序：重新聚合後 close 值更正", "17500", "17650")]


def test_apply_correction_without_existing_row_raises(repo: SqliteBarRecordRepository) -> None:
    record = _record(_ts(2026, 9, 16, 8, 45), _ts(2026, 9, 16, 9, 45))
    with pytest.raises(BarRecordNotFoundError):
        repo.apply_correction(record, reason="no prior row exists")


def test_query_range_filters_by_trading_day(repo: SqliteBarRecordRepository) -> None:
    inside = _record(_ts(2026, 9, 16, 8, 45), _ts(2026, 9, 16, 9, 45))
    outside = _record(_ts(2026, 6, 1, 8, 45), _ts(2026, 6, 1, 9, 45))
    repo.upsert_closed_bar(inside)
    repo.upsert_closed_bar(outside)

    found = repo.query_range(
        _INSTRUMENT,
        _CONTRACT,
        BarPeriod.SIXTY_MINUTE,
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
    )
    assert [r.bar.start for r in found] == [inside.bar.start]


def test_earliest_and_latest_recorded_at_none_when_empty(repo: SqliteBarRecordRepository) -> None:
    assert repo.earliest_recorded_at(_INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE) is None
    assert repo.latest_recorded_at(_INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE) is None


def test_earliest_and_latest_recorded_at(repo: SqliteBarRecordRepository) -> None:
    early = _record(_ts(2026, 9, 16, 8, 45), _ts(2026, 9, 16, 9, 45))
    late = _record(_ts(2026, 9, 16, 9, 45), _ts(2026, 9, 16, 10, 45))
    repo.upsert_closed_bar(early)
    repo.upsert_closed_bar(late)

    assert (
        repo.earliest_recorded_at(_INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE) == early.bar.start
    )
    assert repo.latest_recorded_at(_INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE) == late.bar.start


def test_delete_before_only_removes_older_bars(repo: SqliteBarRecordRepository) -> None:
    old = _record(_ts(2026, 5, 1, 8, 45), _ts(2026, 5, 1, 9, 45))
    recent = _record(_ts(2026, 9, 16, 8, 45), _ts(2026, 9, 16, 9, 45))
    repo.upsert_closed_bar(old)
    repo.upsert_closed_bar(recent)

    summary = repo.delete_before(date(2026, 6, 18), ran_at=_ts(2026, 8, 18, 0, 0))
    assert summary.deleted_count == 1
    assert summary.cutoff_trading_day == date(2026, 6, 18)

    remaining = repo.list_recent(
        _INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE, since_trading_day=date(2026, 1, 1)
    )
    assert [r.bar.start for r in remaining] == [recent.bar.start]


def test_identity_is_scoped_by_contract(repo: SqliteBarRecordRepository) -> None:
    start, end = _ts(2026, 9, 16, 8, 45), _ts(2026, 9, 16, 9, 45)
    a = _record(start, end, price="17500", contract=_CONTRACT)
    b = _record(start, end, price="18000", contract=_OTHER_CONTRACT)
    assert repo.upsert_closed_bar(a) is BarUpsertOutcome.INSERTED
    # Not a conflict — different contract means a different identity.
    assert repo.upsert_closed_bar(b) is BarUpsertOutcome.INSERTED

    a_found = repo.list_recent(
        _INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE, since_trading_day=date(2026, 1, 1)
    )
    b_found = repo.list_recent(
        _INSTRUMENT, _OTHER_CONTRACT, BarPeriod.SIXTY_MINUTE, since_trading_day=date(2026, 1, 1)
    )
    assert a_found[0].bar.open.amount == Decimal("17500")
    assert b_found[0].bar.open.amount == Decimal("18000")


def test_decimal_and_timestamp_round_trip_exactly(repo: SqliteBarRecordRepository) -> None:
    start = _ts(2026, 9, 16, 8, 45)
    end = _ts(2026, 9, 16, 9, 45)
    record = _record(start, end, price="17500.5")
    repo.upsert_closed_bar(record)

    found = repo.list_recent(
        _INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE, since_trading_day=date(2026, 1, 1)
    )[0]
    assert found.bar.open.amount == Decimal("17500.5")
    assert found.bar.start.value.utcoffset() == start.value.utcoffset()
    assert found.bar.start.value == start.value


def test_is_gap_recovery_round_trips(repo: SqliteBarRecordRepository) -> None:
    record = _record(_ts(2026, 9, 16, 8, 45), _ts(2026, 9, 16, 9, 45), is_gap_recovery=True)
    repo.upsert_closed_bar(record)
    found = repo.list_recent(
        _INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE, since_trading_day=date(2026, 1, 1)
    )[0]
    assert found.is_gap_recovery is True


def test_get_one_returns_none_when_absent(repo: SqliteBarRecordRepository) -> None:
    start = _ts(2026, 9, 16, 8, 45)
    assert repo.get_one(_INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE, start) is None


def test_get_one_returns_stored_record(repo: SqliteBarRecordRepository) -> None:
    start, end = _ts(2026, 9, 16, 8, 45), _ts(2026, 9, 16, 9, 45)
    record = _record(start, end, price="17500")
    repo.upsert_closed_bar(record)

    found = repo.get_one(_INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE, start)
    assert found is not None
    assert found.bar.open.amount == Decimal("17500")


def _yfinance_record(start: Timestamp, end: Timestamp, *, price: str) -> BarRecord:
    return BarRecord(
        bar=_bar(start, end, price=price),
        period=BarPeriod.SIXTY_MINUTE,
        trading_day=start.value.date(),
        session=MarketSession.DAY,
        source=BarDataSource.BACKFILLED_FROM_YFINANCE,
        is_gap_recovery=False,
        created_at=start,
        updated_at=start,
    )


def test_record_conflict_and_list_conflicted_trading_days(
    repo: SqliteBarRecordRepository,
) -> None:
    start, end = _ts(2026, 9, 16, 8, 45), _ts(2026, 9, 16, 9, 45)
    existing = _record(start, end, price="17500")
    repo.upsert_closed_bar(existing)
    incoming = _yfinance_record(start, end, price="17999")
    assert repo.upsert_closed_bar(incoming) is BarUpsertOutcome.CONFLICT_REJECTED

    audit = BarConflictAudit(existing=existing, incoming=incoming, detected_at=Timestamp.now())
    repo.record_conflict(audit)

    conflicted = repo.list_conflicted_trading_days(
        _INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE, since_trading_day=date(2026, 1, 1)
    )
    assert conflicted == frozenset({start.value.date()})

    # White-box check of the audit row's stored summary of both sides.
    row = repo._conn.execute(
        "SELECT existing_source, existing_close, incoming_source, incoming_close "
        "FROM bar_backfill_conflicts"
    ).fetchone()
    assert row == (
        BarDataSource.POLLED_FROM_YFINANCE.value,
        "17500",
        BarDataSource.BACKFILLED_FROM_YFINANCE.value,
        "17999",
    )


def test_list_conflicted_trading_days_empty_when_none_recorded(
    repo: SqliteBarRecordRepository,
) -> None:
    conflicted = repo.list_conflicted_trading_days(
        _INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE, since_trading_day=date(2026, 1, 1)
    )
    assert conflicted == frozenset()


def test_list_conflicted_trading_days_respects_since_trading_day(
    repo: SqliteBarRecordRepository,
) -> None:
    start, end = _ts(2026, 5, 1, 8, 45), _ts(2026, 5, 1, 9, 45)
    existing = _record(start, end, price="17500")
    repo.upsert_closed_bar(existing)
    incoming = _yfinance_record(start, end, price="17999")
    repo.upsert_closed_bar(incoming)
    repo.record_conflict(
        BarConflictAudit(existing=existing, incoming=incoming, detected_at=Timestamp.now())
    )

    conflicted = repo.list_conflicted_trading_days(
        _INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE, since_trading_day=date(2026, 6, 18)
    )
    assert conflicted == frozenset()
