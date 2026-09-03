"""Helpers every call site touching credentials, account numbers, or other sensitive
values must use before the value reaches `log_event`'s `fields`.

Every implementation prompt's "除錯日誌需求" repeats the same boundary: account
numbers may be masked, never printed in full; passwords, certificates, and other
secrets must never appear at all — presence/absence only.
"""

from __future__ import annotations

_UNMASKED_SUFFIX_LENGTH = 4


def mask_account(account_no: str) -> str:
    """Masks all but the last 4 characters, e.g. `"1234567"` -> `"***4567"`. Short
    values are masked in full rather than accidentally revealing everything."""
    if len(account_no) <= _UNMASKED_SUFFIX_LENGTH:
        return "*" * len(account_no)
    return "*" * (len(account_no) - _UNMASKED_SUFFIX_LENGTH) + account_no[-_UNMASKED_SUFFIX_LENGTH:]


def field_present(value: object | None) -> bool:
    """Whether a possibly-sensitive value is present, for logging "存在／缺少"
    without ever logging the value itself. A blank/whitespace-only string counts as
    absent."""
    if value is None:
        return False
    if isinstance(value, str):
        return len(value.strip()) > 0
    return True
