# 10. Changelog

Chronological record of the architecture gate review and the C1-C6
correction process. Entries reflect the order in which work was approved,
implemented, audited, and frozen against `turtle_backtest.py`.

## Architecture Gate Review

- Chief Quant / Chief Software Architect review conducted.
- Verdict: architecture frozen (dynamic-universe engine, feature
  registry, purged/embargoed validation stack, portfolio/risk layer
  structurally sound); implementation/research validity not cleared
  pending six critical corrections (C1-C6).
- Overarching gate condition recorded: no statistically demonstrated
  OHLCV edge (meta-label AUC approximately 0.4837); live capital gated on
  post-fix out-of-sample performance beating regime-gated buy-and-hold.

## C1 -- Transaction Cost Correction

- Implemented: raw `FEE + SLIP` subtraction replaced with
  `(FEE + SLIP) / stop_distance` at 14 sites across three engines and two
  label simulators.
- Verified: zero remaining raw-subtraction sites; portfolio metrics
  confirmed to consume the corrected value automatically.
- Frozen.

## C2 -- Intrabar High/Low Execution

- Implemented: optional `hilo_dated` parameter added to all three
  engines; stop checked against Low, targets against High, stop given
  precedence on same-candle conflict; close-only fallback preserved
  exactly when High/Low data is unavailable.
- Verified against real historical CSV data: no look-ahead, deterministic
  precedence, correct fallback identity.
- Frozen.

## C3 -- Per-Position Accounting

- Implemented (v1): `aggregate_positions()` added and wired into
  reporting and equity-curve computation.
- Audited: found that routing the equity curve through
  `aggregate_positions()` discarded the T1 partial's true realization
  date, collapsing two dated cashflows into one and distorting Sharpe/
  Sortino/Max Drawdown.
- Implemented (v2, revision): `aggregate_positions()` restricted to
  reporting (`report()`, `summarize()`) only; `build_portfolio_equity_curve()`
  reverted to the raw chronological per-leg cashflow list.
- Verified: portfolio metrics reverted exactly to pre-C3 values;
  reporting-level win rate and position count corrected.
- Frozen.

## C4 -- Correlation-Aware Portfolio Risk

- Implemented (v1): static, full-sample correlation matrix (via the
  existing `compute_portfolio_correlation()`) used to scale position
  weight when concurrent open positions were highly correlated.
- Audited: static full-sample correlation computation classified as a
  critical look-ahead defect (uses data not available at each historical
  decision point).
- Implemented (v2, revision): correlation recomputed as a point-in-time
  expanding window at each trade's own entry day, using only already-
  closed trades, cached per unique entry day.
- Verified: zero trades used in a correlation window before they closed;
  zero future-dated trades present in any historical window checked.
- Frozen.

## C5 -- Survivorship Mitigation

- Implemented (v1): daily median-liquidity entry filter added inside
  `run_dynamic`/`run_dynamic_attr`, restricting new entries to the
  top-half of each day's tradeable universe by trailing dollar volume.
- Audited: determined to be an undisclosed strategy/universe redesign
  (trade count reduced by more than half) rather than a survivorship
  correction; rejected.
- Implemented (v2, revision): all engine-level filter code reverted;
  `run_survivorship_dependence_report()` added as a read-only, post-hoc
  diagnostic outside the trading engine.
- Verified: trade list and every portfolio metric identical to the
  C4-frozen baseline.
- Frozen.

## C6 -- Live Data Fail-Safe

- Implemented: `compute_data_integrity_flags()` added (malformed price/
  volume, dropped-candle gap detection); wired into `run_dynamic`/
  `run_dynamic_attr` as an additional entry-blocking, exit-preserving
  day-skip gate with a critical alert on affected open positions.
- Verified: zero false positives on real historical data (30 genuine
  gaps detected, none altering trade count or portfolio metrics);
  correct triggering demonstrated via an injected malformed-price test;
  no look-ahead confirmed by code inspection.
- Final verification: "skip evaluation, preserve position" found correct
  for backtesting and paper trading, insufficient for live trading due to
  the absence of a broker-state reconciliation mechanism. Recorded as an
  unresolved item explicitly out of scope for this engine (see
  `07_Architecture_Freeze.md`).
- Frozen (backtest scope).

## Architecture Freeze Declared

- All six corrections frozen. Trading strategy, indicators, validation
  methodology, feature framework, portfolio framework, and architecture
  locked against further modification within Turtle QTS Research Engine.
- Allowed future work scoped to: critical defect corrections meeting the
  same bar as C1-C6, a separate live execution layer project, and a
  separate Stock Research Engine project.

## Documentation

- Full documentation set generated (`README.md`, `01_Architecture.md`
  through `10_CHANGELOG.md`, `PROJECT_INDEX.md`) against the final frozen
  source, with no code modification.
