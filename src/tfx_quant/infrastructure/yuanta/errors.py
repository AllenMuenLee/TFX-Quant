"""Yuanta adapter exceptions.

Not `DomainError` subclasses — these are infrastructure-layer failures (missing
credentials, an unregistered COM control, a login timeout), not domain-rule
violations. Every message here is meant to be shown to the operator as-is, so each one
must be actionable and must never interpolate a secret (password, full account
number) into the text.
"""

from __future__ import annotations


class YuantaSessionError(Exception):
    """Base class for all `infrastructure.yuanta` session-lifecycle failures."""


class PreflightCheckFailed(YuantaSessionError):
    """One or more startup preflight checks failed.

    Raised with an aggregated, actionable Chinese message listing every failed check
    (not just the first one) so the operator can fix everything in one pass.
    """


class CredentialsUnavailableError(YuantaSessionError):
    """The configured credential source could not resolve a user ID or password.

    Never includes the attempted value in the message — only which name/entry was
    missing.
    """


class LoginTimeoutError(YuantaSessionError):
    """No login callback arrived within the configured timeout."""


class AccountSelectionError(YuantaSessionError):
    """`select_account()` was called with an account not present in `accounts`, or
    the session reached a point requiring a unique account with none resolved."""


class InstrumentMasterFileError(YuantaSessionError):
    """The controlled 商品主檔 JSON file is missing, malformed, or internally
    inconsistent (duplicate entries, bad field types) — raised at load time so a bad
    file fails loudly at startup rather than surfacing as a confusing lookup miss
    later."""


class MarketDataSubscriptionError(YuantaSessionError):
    """A synchronous `AddMktReg`/`DelMktReg` call was attempted while the broker
    session isn't `READY`, or the vendor synchronously rejected the registration
    (non-zero `RegErrCode`)."""


class MarketDataParseError(YuantaSessionError):
    """An `OnGetMktAll` push's fields couldn't be parsed into typed values (blank
    symbol, non-numeric price/quantity, malformed `MatchTime`) — see
    `market_data_parsing.py`."""
