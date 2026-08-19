# Secrets management

Broker credentials (Yuanta login ID/password, API keys) must never appear in source
code, `pyproject.toml`, `settings.example.json`, any committed config file, or general
application logs.

## What `TradingSettings` holds instead

`application/settings/trading_settings.py`'s `TradingSettings` model has **no
credential field at all**. It only holds non-secret operational config:
`account_alias` (a human label like `"primary"`, never the raw broker account
number), `environment`, `selected_instrument`, `contract_selection_mode`,
`timezone_id`, `eod_flatten_local_time`, `max_net_lots`, `use_mock`.
`src/tfx_quant/desktop/settings.example.json` is safe to commit because of this — it
never needs a `.local`/`.gitignore`d variant to hide a secret. Note that
`TradingSettings.environment` is no longer what the real (non-mock) broker session
actually connects with — that's chosen per login attempt on the login screen (default
TEST); this field remains only as informational config shown elsewhere in the UI.

## Where credentials actually come from (Feature 02+)

Feature 02's extension (`login-input-implementation-prompt.md`) replaced the original
env-var-based flow with a login screen — no environment variable or manual Windows
Credential Manager setup is required before first use:

- 元大歸戶 ID and password are typed into `desktop/login_dialog.py`'s `LoginDialog`
  each session, and handed to `IBrokerSession.start()` as a `LoginRequest`
  (`application/ports/broker_session.py`) — never persisted anywhere by default.
- Checking "記住歸戶 ID" persists only the ID (non-secret) to a local per-user JSON
  file (`infrastructure/yuanta/login_preferences.py`,
  `%LOCALAPPDATA%/tfx_quant/login_prefs.json`) — never the password.
- Checking "安全儲存密碼" persists the password to Windows Credential Manager via
  `keyring.set_password("tfx_quant.yuanta", user_id, password)`
  (`infrastructure/yuanta/credentials.py`'s `store_password`) only after a successful
  login. Left unchecked, the password lives only in memory for the current session
  and is cleared on logout (`BrokerSessionOrchestrator.stop()`). The login screen's
  "清除已儲存密碼" button removes it again
  (`credentials.clear_stored_password`/`keyring.delete_password`).
- The previously-selected futures account number is remembered the same non-secret
  way (`login_preferences.save_remembered_account_no`), read back as
  `BrokerSessionOrchestrator`'s `account_no_hint` on the next run — the orchestrator
  still requires the hint to match an account the broker's login response actually
  returned before auto-selecting it (`session_orchestrator._resolve_account`).

`BrokerCredentials.password` and `LoginRequest.password` are both
`pydantic.SecretStr` so `repr()`/`str()`/logging never print the raw value
(`SecretStr("**********")`). The SPARK API certificate password
(`credentials.ensure_certificate_imported`) is piped to `certutil` via stdin, never
passed as a CLI argument — a CLI arg would appear in that process's argument list,
visible to other processes/audit tools on the machine (see
`infrastructure/yuanta/README.md`).

## Rules enforced going forward

- Never **log** a full account number, password, or API key — `compute_readiness()`
  (`desktop/composition.py`) only ever returns `account_alias` and boolean/label
  readiness state, confirmed by `tests/desktop/
  test_composition.py::test_compute_readiness_never_includes_account_number_or_secrets`.
  This is about persistent logs/console output specifically, not the local operator's
  own screen: `ReadinessFrame`'s account picker (Feature 02) necessarily *displays*
  the branch/account/sub-account of each futures account login returns, since the
  operator has no other way to distinguish between them when more than one comes back
  (see `docs/adr/0004-broker-session-architecture.md`) — never write that same data to
  a log file.
- `.gitignore` excludes `*.env`, `.env.*`, `settings.local.json`,
  `settings.*.local.json`, and any `*.db`/`*.sqlite3` file (a local DB could
  accumulate account numbers via position/order history in later features).
- The vendor API packages themselves (`交易API元件及說明文件/`,
  `行情API元件及說明文件/`) are proprietary and gitignored — never committed.
