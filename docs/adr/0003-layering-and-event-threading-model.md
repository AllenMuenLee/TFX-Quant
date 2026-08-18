# ADR 0003 — Package layering and event/threading model

## Status

Accepted (Feature 01).

## Context

`implementation prompt/01-solution-foundation/implementation-prompt.md` requires
layering the system into `domain`, `application`, `infrastructure.yuanta`,
`persistence`, `desktop`, `tests`, with dependencies pointing toward `domain`; a
single event coordinator that serializes broker-callback/UI/strategy-thread traffic;
and dependency injection everywhere so time, identity, database, and broker API are
all replaceable in tests.

Python has no compiler-enforced project-reference graph like a .NET solution's
`.csproj` references — importing across "layers" is not a build error by default.

## Decision

- **Single distribution, `src/tfx_quant/` src-layout**, with subpackages `domain`,
  `application` (+ `application.ports`, `application.events`, `application.safety`,
  `application.settings`), `infrastructure` (+ `infrastructure.yuanta`),
  `persistence`, `desktop`. This literally matches the prompt's package names, using
  Python's dotted-package convention for `infrastructure.yuanta`.
- **`import-linter`** (`lint-imports`, run in CI) mechanically enforces the
  dependency direction via `forbidden` contracts in `pyproject.toml`
  (`[[tool.importlinter.contracts]]`): `domain` cannot import `application` /
  `infrastructure` / `persistence` / `desktop`; `application` cannot import
  `infrastructure` / `persistence` / `desktop`; `infrastructure` cannot import
  `persistence` / `desktop`; `persistence` cannot import `infrastructure` / `desktop`.
  This is the Python equivalent of .NET project references — a real, CI-enforced
  architecture test instead of a convention people can silently violate.
- **No DI framework.** Plain constructor injection + a single composition root
  (`desktop/composition.py`, `build_services()`) that wires concrete implementations
  behind `application.ports` Protocols (`Clock`, `IdGenerator`, `TradeGatewayPort`,
  `QuoteGatewayPort`). Mock vs. real Yuanta gateways are chosen off
  `TradingSettings.use_mock` (default `True`), mirroring the prior .NET decision's
  `useMock: true` default.
- **`EventCoordinator`** (`application/events/event_coordinator.py`): a single
  `queue.Queue` drained by exactly one dedicated daemon consumer thread.
  `publish(event)` is thread-safe and callable from any thread (a future COM callback
  thread, the strategy loop, etc.); every subscriber handler runs on that one
  consumer thread only, strictly in publish order, so no two handlers ever execute
  concurrently with each other. A handler exception is caught and re-published as an
  `UnhandledHandlerError` event rather than crashing the loop or vanishing silently —
  this is the mechanism the global rule "任何未捕捉例外應轉入安全暫停" routes
  through: application code subscribes to `UnhandledHandlerError` and drives the
  `StrategyStateMachine` toward `Faulted`.
- **`StrategyStateMachine`** (`domain/strategy_state.py`): states `Stopped`,
  `Starting`, `Running`, `PausedSafe`, `Stopping`, `Faulted`. `Running` is reachable
  **only** from `Starting`, so entering live trading always passes through
  `StartupSafetyGate.can_enter_running()` (`application/safety/`), which additionally
  requires every item of `SafetyChecklist` (API logged in, account confirmed, market
  data valid, order query completed, position synced, no unknown orders, user pressed
  start) to be true — no partial bypass.

## Consequences

- Domain types are stdlib-only frozen `dataclasses` (no pydantic/pandas/etc.
  dependency inside `domain/`), keeping the innermost layer dependency-free as the
  layering diagram implies.
- `TradingSettings` uses `pydantic` v2 in `application/settings/` for strong typing +
  validation; `validate_startup()` is the one place a malformed config fails loudly
  with a clear combined message (wrong time zone, wrong flatten time, lot cap above 2,
  undefined instrument, incomplete manual contract selection).
- CI runs `ruff`, `mypy`, `lint-imports`, and `pytest` — no Yuanta API/OCX
  installation is required for any of them, since Feature 01 only wires mock gateways.
