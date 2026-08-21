from __future__ import annotations

import pytest

from tfx_quant.domain.position_reconciliation import (
    SUSPECTED_CAUSE_HYPOTHESES,
    DiscrepancyKind,
    ManualSyncPreflight,
    classify_discrepancy,
)
from tfx_quant.domain.quantity import NetPosition


@pytest.mark.parametrize(
    "expected_lots,actual_lots,want",
    [
        (0, 0, DiscrepancyKind.NONE),
        (1, 1, DiscrepancyKind.NONE),
        (-2, -2, DiscrepancyKind.NONE),
        (0, 1, DiscrepancyKind.DIRECTION),
        (0, -1, DiscrepancyKind.DIRECTION),
        (1, -1, DiscrepancyKind.DIRECTION),
        (1, 0, DiscrepancyKind.DIRECTION),
        (-1, 0, DiscrepancyKind.DIRECTION),
        (1, 2, DiscrepancyKind.QUANTITY),
        (-1, -2, DiscrepancyKind.QUANTITY),
        (2, 1, DiscrepancyKind.QUANTITY),
    ],
)
def test_classify_discrepancy(expected_lots: int, actual_lots: int, want: DiscrepancyKind) -> None:
    result = classify_discrepancy(NetPosition(expected_lots), NetPosition(actual_lots))
    assert result is want


def test_suspected_cause_hypotheses_lists_every_candidate_honestly() -> None:
    """This system cannot actually distinguish which caused an out-of-band position
    change — the constant must always list every hypothesis, never narrow to one."""
    assert len(SUSPECTED_CAUSE_HYPOTHESES) == 3
    assert len(set(SUSPECTED_CAUSE_HYPOTHESES)) == 3


def test_manual_sync_preflight_allowed_only_when_no_active_or_unknown_orders() -> None:
    assert ManualSyncPreflight(has_active_orders=False, has_unknown_orders=False).allowed is True
    assert ManualSyncPreflight(has_active_orders=True, has_unknown_orders=False).allowed is False
    assert ManualSyncPreflight(has_active_orders=False, has_unknown_orders=True).allowed is False
    assert ManualSyncPreflight(has_active_orders=True, has_unknown_orders=True).allowed is False
