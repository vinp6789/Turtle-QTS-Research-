# 04. Portfolio Engine

## Portfolio Construction

The portfolio engine consumes the pooled, chronological trade-leg list
produced by the frozen entry/exit engines and constructs a single
compounded equity curve. Construction happens strictly after all
trading decisions have been made; it does not feed back into entries,
exits, stops, targets, or conviction scoring.

`build_portfolio_equity_curve(trades, initial_capital, risk_pct_per_trade,
sizing_mode, heat_cap, correlation_aware)` sorts trades by entry day and,
for each trade in chronological order, computes a position weight,
applies that trade's R-multiple against current (compounding, not fixed
initial) capital at `risk_pct_per_trade * weight`, and records the
resulting equity point. Each trade leg (including a T1-half partial and
its position's later closing leg) is applied exactly once, at its own
true historical date -- see C3 in `06_C1_C6_Corrections.md`.

## Position Sizing

`compute_position_weight(t, sizing_mode, avg_stop_d, avg_conv)` returns a
multiplier on the base `risk_pct_per_trade`, clamped to [0.3x, 2.0x]:

- `"fixed"` -- 1.0 for every trade (byte-identical to the original
  baseline behavior).
- `"vol_targeted"` -- inversely proportional to the trade's own stop
  width relative to the portfolio average stop width, so every trade
  risks roughly the same dollar amount regardless of the asset's
  volatility at entry.
- `"conviction_weighted"` -- proportional to the trade's own conviction
  score relative to the portfolio average conviction score.

## Risk Profiles

`RISK_PROFILES` defines three named configurations:

| Profile | Risk/trade | Max positions | Sizing mode | Heat cap | Ruin threshold |
|---|---|---|---|---|---|
| GROWTH | 2.0% | 4 | conviction_weighted | 10% | 50% of starting capital |
| BALANCED | 1.0% | 3 | fixed | 5% | 60% of starting capital |
| CAPITAL_PRESERVATION | 0.5% | 2 | fixed | 2% | 75% of starting capital |

`run_risk_profile_comparison` runs all three against the same frozen
entry logic and reports CAGR, Sharpe, Sortino, Calmar, Max Drawdown,
Profit Factor, Recovery Factor, and Risk of Ruin for each, plus an
empirical check on whether conviction-weighted sizing is justified for
GROWTH.

## Correlation Adjustment

`compute_portfolio_correlation(trades)` buckets trades into 90-day
windows, computes each asset's mean return per bucket, and correlates
asset pairs (`pearson_r`) -- the existing, unmodified correlation
machinery. As implemented in the frozen `build_portfolio_equity_curve`
(C4, revised), correlation is recomputed as a point-in-time expanding
window at each trade's own entry day, using only trades that had already
closed by that date. When a new position's symbol has an absolute
correlation above 0.5 (the same "HIGH correlation" threshold
`print_correlation_report` already flags) with a currently-open symbol,
its weight is divided by `(1 + count of currently-open correlated
positions)`. This never skips a trade; it only reduces weight. See C4 in
`06_C1_C6_Corrections.md` for the full correction history, including the
rejected static full-sample version and the audited point-in-time
replacement.

## Heat Cap

An optional `heat_cap` (e.g., 0.06 = 6% of capital) caps total concurrent
risk across all simultaneously open positions. When a new trade's entry
would push cumulative open risk above the cap, its weight is scaled down
(not skipped) so the cap is respected exactly. `None` (default) preserves
unlimited concurrent risk with no cap.

## Portfolio Metrics

`compute_portfolio_metrics(trades, initial_capital, risk_pct_per_trade,
days_per_year, sizing_mode, heat_cap, correlation_aware)` builds the
equity curve and derives:

- **CAGR** -- from final equity, initial capital, and elapsed years
  (span measured on the raw per-leg trade list).
- **Sharpe / Sortino** -- mean and downside-deviation of per-trade
  compounded returns, annualized by trade frequency (trades/year), the
  standard approach for a swing system without a fixed daily rebalance
  schedule.
- **Max Drawdown %** -- largest peak-to-trough decline on the real
  compounded equity curve.
- **Calmar** -- CAGR divided by Max Drawdown.
- **Profit Factor** -- gross gains divided by gross losses across
  per-trade compounded returns.
- **Recovery Factor** -- net profit divided by (Max Drawdown times
  initial capital).

## Monte Carlo

`run_monte_carlo_robustness(trades, initial_capital, risk_pct_per_trade,
sizing_mode, n_bootstrap, seed, heat_cap)` bootstrap-resamples the trade
set `n_bootstrap` times, recomputing `compute_portfolio_metrics()` on
each resample, and tracks each resample's CAGR, Sharpe, Sortino, Calmar,
Max Drawdown, Profit Factor, and minimum equity fraction ever reached.
`print_monte_carlo_report` displays baseline vs. Monte Carlo mean, 5th/95th
percentile, and probability-of-negative-outcome for each metric.

## Risk of Ruin

`compute_risk_of_ruin(mc_results, ruin_threshold)` computes, from the
Monte Carlo minimum-equity-fraction distribution, the fraction of
simulated paths that ever fell to or below `ruin_threshold` of starting
capital at any point along the path -- the standard definition of risk of
ruin for a fixed-fractional-risk sizing scheme.

## Performance Reporting

`run_portfolio_report(histories_dated, registry, btc_id, n_folds_requested,
hilo_dated, max_positions, initial_capital, risk_pct_per_trade,
sizing_mode, trades_override)` generates the pooled, walk-forward,
out-of-sample trade set (via `run_dynamic_attr` on each fold's test-only
window) and prints the full portfolio performance report: trade count,
span, trades/year, final equity, net P&L, CAGR, Sharpe, Sortino, Max
Drawdown, Calmar, Profit Factor, and Recovery Factor.
`run_autonomous_portfolio_optimization` runs the full diagnostic queue
(Monte Carlo, correlation analysis, sizing-mode comparison, position-cap
sensitivity, transaction-cost sensitivity) against the frozen baseline
with an explicit keep/reject decision for each experiment.
