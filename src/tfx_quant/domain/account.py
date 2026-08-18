"""交易帳號 — the broker account identity an order/position/fill belongs to."""

from __future__ import annotations

from dataclasses import dataclass

from tfx_quant.domain.errors import InvalidAccountError


@dataclass(frozen=True, slots=True)
class TradingAccount:
    """Branch/account/sub-account triple identifying a Yuanta futures account.

    This is an identity value object, never a credential — no password or API key
    belongs here. See docs/secrets-management.md.
    """

    branch_id: str
    account_no: str
    sub_account: str = ""

    def __post_init__(self) -> None:
        if not self.branch_id.strip():
            raise InvalidAccountError("branch_id must not be empty")
        if not self.account_no.strip():
            raise InvalidAccountError("account_no must not be empty")
