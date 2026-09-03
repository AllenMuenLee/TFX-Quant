"""Yuanta adapter exceptions.

Not `DomainError` subclasses — these are infrastructure-layer failures (missing
credentials, a login timeout, a malformed vendor push), not domain-rule violations.
Every message here is meant to be shown to the operator as-is, so each one must be
actionable and must never interpolate a secret (password, full account number) into
the text.
"""

from __future__ import annotations


class YuantaSessionError(Exception):
    """Base class for all `infrastructure.yuanta` session-lifecycle failures."""


class PreflightCheckFailed(YuantaSessionError):
    """One or more startup preflight checks failed.

    Raised with an aggregated, actionable Chinese message listing every failed check
    (not just the first one) so the operator can fix everything in one pass.
    """


class LoginTimeoutError(YuantaSessionError):
    """No `Login` result arrived via `OnResponse` within the configured timeout."""


class AccountSelectionError(YuantaSessionError):
    """`select_account()` was called with an account not present in `accounts`, or
    the session reached a point requiring a unique account with none resolved."""


class CertificateImportError(YuantaSessionError):
    """`credentials.ensure_certificate_imported` failed — the certificate file was
    missing/invalid, the password was wrong, or `certutil` itself failed."""


class InstrumentMasterFileError(YuantaSessionError):
    """The controlled 商品主檔 JSON file is missing, malformed, or internally
    inconsistent (duplicate entries, bad field types) — raised at load time so a bad
    file fails loudly at startup rather than surfacing as a confusing lookup miss
    later."""
