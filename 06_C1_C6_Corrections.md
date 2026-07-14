# 06. C1-C6 Corrections

This document records the six approved critical corrections identified by
the architecture gate review and implemented against the frozen
architecture. Each correction was implemented, mathematically audited,
and (where the audit found a defect in the implementation itself)
revised, before being declared frozen.

---

## C1 -- Transaction Cost Correction

**Problem.** The R-multiple computation subtracted a raw fee-plus-
slippage fraction (`FEE + SLIP = 0.002`) directly from an R-multiple,
instead of converting that cost into R-units first.

**Root cause.** `r = (exit/entry - 1) / stop_d * frac - FEE - SLIP`
treats a price-return fraction as if it were already expressed in
R-multiple units. True cost in R-units is `(FEE + SLIP) / stop_d`,
4-12x larger than the raw fraction given the strategy's 8-25% stop-
distance range.

**Implementation.** Replaced `- FEE - SLIP` with `- (FEE + SLIP) /
<stop distance>` at all 14 occurrences across the three duplicated
trading engines (`run`, `run_dynamic`, `run_dynamic_attr`) and the two
label simulators (`simulate_trade_outcome`,
`simulate_trade_outcome_diagnostic`).

**Validation.** Full re-grep confirmed zero remaining raw
`- FEE - SLIP` occurrences. Portfolio-metric consumption confirmed:
`build_portfolio_equity_curve` reads the corrected `r` field directly,
so the fix propagates to every downstream metric automatically. Measured
per-leg cost ratio matched the predicted 4.0x-12.5x range exactly on the
synthetic regression suite.

**Result.** On the synthetic regression suite: trade count unchanged
(204), win rate 46.6% to 46.1%, avg R +0.04 to +0.02. Direction confirmed
correct (an estimate-correcting fix should lower, not raise, the
measured edge).

---

## C2 -- Intrabar High/Low Execution

**Problem.** Stops and targets were evaluated against daily closes only;
gap-throughs and intraday touches that reverted by close were invisible
to the backtest, understating left-tail losses and drawdown.

**Root cause.** The exit-check block compared the day's close price
against the stop/T1/T2 levels; High/Low data, though loadable
(`load_csv_hilo`), was never wired into the exit path.

**Implementation.** Added optional `hilo_dated` parameter to `run`,
`run_dynamic`, and `run_dynamic_attr`. Exit checks use the day's Low for
the stop test and High for the T1/T2 tests when High/Low data is
available and date-aligned; falls back to close otherwise (bit-identical
to pre-C2 behavior). Stop is checked before any target in the same
`elif` chain, so a same-candle conflict always resolves to the stop.
Fill price remains the exact barrier level.

**Validation.** Regression suite (`--synthetic`, no High/Low data)
confirmed byte-identical output. On real historical CSV data: 44
additional stop-outs, 9 additional target hits, 1 same-candle conflict
(resolved to stop, 0 precedence violations), confirmed via direct
instrumentation of the live function. No look-ahead: each day's exit
check uses only that day's own High/Low index. Same-day re-entry after
an intraday stop confirmed impossible, both structurally (the `if sym in
open_pos` / `elif` branch structure) and empirically (0 violations across
690 real closing legs).

**Result.** On real historical data: trade count 787 to 882, CAGR 24.07%
to 26.77%, Sharpe 1.798 to 1.854, Max Drawdown 32.10% to 33.03%. Result
direction (higher CAGR/Sharpe alongside higher MaxDD) was data-dependent,
not guaranteed, and explained by earlier intrabar stop-outs replacing
larger eventual signal-loss-exit losses in this dataset.

---

## C3 -- Per-Position Accounting

**Problem.** A T1 partial exit booked a separate, always-positive
`t1_half` trade record; win-rate and per-trade statistics counted it as
an independent win. The equity curve applied `risk_pct_per_trade` once
per leg, consuming two compounding steps for one real position.

**Root cause.** Reporting and equity-curve code consumed the raw per-leg
trade list without distinguishing a partial exit from a completed
position.

**Implementation (first pass).** Added `aggregate_positions()`, merging a
T1-half leg and its position's closing leg into one completed-position
record, keyed by `(sym, entry_day)`. Wired into `report()`, `summarize()`,
and `build_portfolio_equity_curve()`.

**Audit finding.** Mathematical audit found that routing
`build_portfolio_equity_curve()` through `aggregate_positions()` merged
both legs' cashflow into a single event dated at the position's final
exit day, discarding the true T1 realization date. Direct comparison
showed this timing loss, not a genuine double-risk bug, explained the
majority of the resulting Sharpe change (approximately 0.32 of a 0.334
delta).

**Implementation (revision).** Removed `aggregate_positions()` from
`build_portfolio_equity_curve()` and reverted `compute_portfolio_metrics`'s
span calculation to the raw per-leg list. `aggregate_positions()` remains
used only in `report()`/`summarize()`.

**Validation.** Confirmed `report()` output position-based (183 completed
positions vs. 204 raw legs on the synthetic suite) while portfolio
metrics (CAGR, Sharpe, Sortino, Max Drawdown, Profit Factor, Recovery
Factor) matched the pre-C3 baseline exactly.

**Result.** Reporting-level win rate corrected (47.62% to 40.87% on real
data, removing the always-positive T1-half inflation); portfolio-level
metrics unchanged from the C2-frozen baseline, since the original
per-leg dated cashflow accounting was already mathematically correct.

---

## C4 -- Correlation-Aware Portfolio Risk

**Problem.** Position sizing treated concurrent positions as
independent, with no correlation adjustment, despite the project's own
correlation-reporting tooling already existing.

**Root cause.** `build_portfolio_equity_curve` computed weight from
`compute_position_weight` and an optional heat cap only; no correlation
term existed.

**Implementation (first pass).** Reused `compute_portfolio_correlation()`
unmodified, computed once from the full trade set, to divide a new
position's weight by `(1 + count of correlated open positions)` when
correlation with an open position exceeded 0.5.

**Audit finding.** Mathematical audit found the correlation matrix was
computed from the entire historical dataset before the chronological
simulation loop began -- a static, full-sample look-ahead. Classified as
critical for the validity of the reported portfolio metrics, though
narrowly scoped (sizing only, no effect on trade selection).

**Implementation (revision).** Moved correlation computation inside the
chronological loop, recomputed as an expanding window using only trades
already closed (`exit_day < entry_day`) as of each new trade's entry day,
cached per unique entry day.

**Validation.** Confirmed 0 trades closed before the very first entry
day, and 0 future-dated trades present in the historical window used at
a mid-backtest checkpoint. Trade count unchanged (882), confirming
selection was untouched.

**Result.** On real historical data (point-in-time version): CAGR 26.77%
to 21.74%, Sharpe 1.854 to 1.803, Max Drawdown 33.03% to 22.90%, Calmar
0.811 to 0.950, Risk of Ruin unchanged at 0.0%.

---

## C5 -- Survivorship Mitigation

**Problem.** The token universe (`DEFAULT_TOKENS`) is a static,
hand-picked list of present-day survivors, not a point-in-time universe;
a material share of any measured edge may be hindsight.

**Root cause.** Universe composition is fixed at the CLI/watchlist level
with no deterministic, data-driven inclusion rule.

**Implementation (first pass, rejected).** Added a daily median-liquidity
entry filter (trailing 90-day dollar volume, top-half-of-universe
admission) directly inside `run_dynamic`/`run_dynamic_attr`.

**Audit finding.** Determined to be a new investment universe/strategy
redesign, not a survivorship correction: it did not touch the hand-picked
token list (the actual source of survivorship bias), instead imposing an
unrelated, previously undocumented liquidity tilt that cut trade count by
more than half. Classified as an intentional strategy redesign, rejected
as out of scope for a "critical defect correction."

**Implementation (revision).** Reverted all engine-level filter code.
Added `run_survivorship_dependence_report()`, a read-only diagnostic
(outside the trading engine) that labels each already-generated trade by
point-in-time liquidity rank and reports the share of trades and realized
R attributable to below-median-liquidity assets, without excluding,
reweighting, or otherwise touching any trade.

**Validation.** Confirmed trade list and every portfolio metric identical
to the C4-frozen baseline (882 legs; CAGR, Sharpe, Sortino, Max Drawdown,
Calmar, Profit Factor, Recovery Factor unchanged).

**Result.** Diagnostic only, no metric change. On real historical data:
52.6% of trades occurred in below-median-liquidity assets, contributing
44.6% of total realized R -- roughly proportionate, not disproportionate,
concentration in this dataset. Residual survivorship bias in the
underlying token-universe selection itself remains and is disclosed in
`08_Known_Limitations.md`.

---

## C6 -- Live Data Fail-Safe

**Problem.** On missing data, the engine leaves a position open with no
mechanism to detect or respond to a live feed/venue outage; a stop cannot
be enforced during such an outage in live execution.

**Root cause.** The existing "no data today" handling (skip the asset,
leave any open position untouched) has no data-quality check preceding
it -- it treats a missing date the same as a malformed or stale one, and
provides no signal distinguishing "not listed yet" from "feed failure."

**Implementation.** Added `compute_data_integrity_flags(dates, closes,
vols, max_gap_days)`, a deterministic, point-in-time, per-asset check
(malformed/non-positive price, malformed/negative volume, or a calendar
gap consistent with a dropped candle). Wired into `run_dynamic` and
`run_dynamic_attr` as an additional day-skip gate: on a flagged day, no
new entry is taken and no exit is evaluated against that day's
unverified price (the stop level itself is not altered); a critical alert
prints when a position is open on a flagged day.

**Validation.** Confirmed zero false positives across all ten real
assets tested (30 flagged days, all traced to genuine multi-day gaps in
the source CSVs), and confirmed trade count and portfolio metrics
identical with the check on vs. off on real data. Demonstrated correct
triggering by injecting a malformed price into a copy of real data:
the corrupted day was flagged, the critical alert printed, and zero
entries were dated on the corrupted day. No look-ahead: confirmed by
code inspection (each flag decision uses only the prior date and the
current index).

**Final verification finding.** A subsequent review determined that
"skip evaluation, preserve position" is correct for backtesting and
paper trading (closed-loop simulations where the engine is the only
actor) but is not sufficient for live trading: during a live outage the
real market and broker state can diverge from the engine's frozen
assumption, and the implementation has no reconciliation mechanism or
distinct uncertain-state flag to detect or correct such a divergence on
data resumption. This is recorded as a remaining, unresolved item for any
future live-execution layer (see `07_Architecture_Freeze.md` and
`08_Known_Limitations.md`); it does not affect the backtester's own
correctness, which was the scope actually implemented and verified.

**Result.** Backtest-scope fail-safe is implemented and verified.
Live-execution-scope reconciliation is explicitly out of scope for this
engine and remains unaddressed.
