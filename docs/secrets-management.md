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
never needs a `.local`/`.gitignore`d variant to hide a secret.

## Where credentials actually come from (Feature 02+)

Feature 02 (Yuanta API session) is what actually logs in and needs a password.
Implemented in `infrastructure/yuanta/credentials.py`:

- 元大歸戶 ID: environment variable `TFX_QUANT_YUANTA_USER_ID`, read by name at
  process startup — never stored anywhere.
- Password: `keyring.get_password("tfx_quant.yuanta", user_id)` — Windows Credential
  Manager, DPAPI-protected. Add it via Windows' own "Credential Manager" control panel
  (a generic credential, network address `tfx_quant.yuanta`, username = the 元大歸戶
  ID) or the `keyring` CLI (`keyring set tfx_quant.yuanta <user_id>`).
- Optional account disambiguation: `TFX_QUANT_YUANTA_ACCOUNT_NO` — same OS-level
  pattern, **not** a `TradingSettings` field (see the rule above), used only to
  auto-select among multiple futures accounts returned by login; see
  `docs/adr/0004-broker-session-architecture.md`.

`BrokerCredentials.password` is a `pydantic.SecretStr` so `repr()`/`str()`/logging
never print the raw value (`SecretStr("**********")`). Never call the vendor trading
OCX's `SetLog(True)` — it logs raw request/response packets to a file, which plausibly
includes the ID/password (see `infrastructure/yuanta/README.md`); this codebase never
calls it.

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
