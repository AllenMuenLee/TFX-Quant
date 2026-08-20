"""Non-secret local login preferences.

Backs the login screen's "記住歸戶 ID" checkbox and the account-selection "記住的帳號
仍須存在於本次回傳清單" requirement (`login-input-implementation-prompt.md`). Deliberately
separate from `credentials.py`: nothing written here is a secret (never a password),
so a plain per-user JSON file is fine — no keyring/DPAPI involved, and this module has
no `keyring` dependency.

The remembered account number is only ever used as a *hint*
(`BrokerSessionOrchestrator`'s `account_no_hint`) — the orchestrator still requires the
hint to match one of the accounts the broker's own login response actually returned
before auto-selecting it (see `session_orchestrator._resolve_account`), so this module
can never cause an arbitrary/stale account to be accepted.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

from tfx_quant.telemetry import get_logger, log_info
from tfx_quant.telemetry.masking import mask_account

_logger = get_logger(__name__)

_PREFS_FILENAME = "login_prefs.json"


@dataclass(frozen=True, slots=True)
class LoginPreferences:
    remembered_user_id: str | None = None
    remembered_account_no: str | None = None


def _prefs_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home()
    return base / "tfx_quant" / _PREFS_FILENAME


def load() -> LoginPreferences:
    """Tolerant of a missing or corrupt file — a bad prefs file must never block
    login, so any read/parse failure is treated the same as "nothing remembered yet"."""
    try:
        raw = json.loads(_prefs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return LoginPreferences()
    if not isinstance(raw, dict):
        return LoginPreferences()
    user_id = raw.get("remembered_user_id")
    account_no = raw.get("remembered_account_no")
    return LoginPreferences(
        remembered_user_id=user_id if isinstance(user_id, str) else None,
        remembered_account_no=account_no if isinstance(account_no, str) else None,
    )


def _save(preferences: LoginPreferences) -> None:
    path = _prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "remembered_user_id": preferences.remembered_user_id,
        "remembered_account_no": preferences.remembered_account_no,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def save_remembered_user_id(user_id: str | None) -> None:
    """`None` clears the remembered ID — the login screen calls this when the operator
    unchecks "記住歸戶 ID"."""
    _save(replace(load(), remembered_user_id=user_id))
    log_info(
        _logger,
        "login_preferences_saved",
        field="remembered_user_id",
        # Non-secret per this module's docstring, but masked anyway for consistency
        # with every other ID/account field's logging policy.
        value_masked=mask_account(user_id) if user_id is not None else None,
    )


def save_remembered_account_no(account_no: str | None) -> None:
    """Called after a successful account selection (auto or explicit) — see
    `desktop/readiness_frame.py`. Read back as `account_no_hint` on the next run's
    `BrokerSessionOrchestrator` construction (`desktop/composition.py`)."""
    _save(replace(load(), remembered_account_no=account_no))
    log_info(
        _logger,
        "login_preferences_saved",
        field="remembered_account_no",
        value_masked=mask_account(account_no) if account_no is not None else None,
    )
