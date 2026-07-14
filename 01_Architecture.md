# 01. Architecture

## Overall Architecture

Turtle QTS Research Engine is organized in layers within a single source
file (`turtle_backtest.py`): data ingestion, a data-integrity gate, a
regime filter, a conviction/feature-scoring engine, an entry/exit engine,
position accounting, a validation layer, and a portfolio engine. Each
layer consumes the output of the layer before it. The entry/exit engine
and the feature registry are frozen (see `07_Architecture_Freeze.md`);
validation and portfolio-reporting functions are read-only consumers of
the trade lists the entry/exit engine produces.

## Engine Flow

There are three trading engines in the source, each sharing the same
entry-gating, exit, stop/target, and position-cap logic:

- `run(histories, ...)` -- legacy shared-index engine. All assets are
  truncated to a common length. Used by `--synthetic` and CoinGecko API
  mode.
- `run_dynamic(histories_dated, ...)` -- dynamic-universe engine. Each
  asset keeps its own native-length history; BTC's date array is the
  global clock. Used by the default CSV-mode report path.
- `run_dynamic_attr(histories_dated, ...)` -- attribution variant of
  `run_dynamic`. Identical entry/exit/stop/target/position-cap logic;
  additionally scores through `conviction_components()` /
  `conviction_components_v2()` so features can be selectively disabled,
  and attaches a `components` (and optionally `raws`/`normalizeds`) dict
  to each trade record. Used by `--portfolio-report`, `--attribute`,
  `--train-model`, `--purged-validation`, and related audit commands.

Each day, per asset: an intrabar-aware exit check runs first for any open
position (stop, then T1, then T2, then signal-loss/regime-flip, in that
precedence order), then entry candidates are collected, ranked by
conviction, and admitted up to any configured position cap.

## Dynamic Universe

`run_dynamic` and `run_dynamic_attr` look up each asset by calendar date
against BTC's own date array (the global clock). An asset becomes part of
a given day's tradeable universe only once (a) it has data for that date
and (b) it has passed its own 210-day warmup, applied per-asset rather
than globally. This avoids truncating older assets to the listing date of
the youngest asset in the universe.

## Feature Framework

`ConvictionFeature` is the base class for a pluggable feature interface
(`raw()`, `normalized()`, `binary()`). Concrete features:
`RelativeStrengthFeature`, `TrendFeature`, `VolumeExpansionFeature`,
`MomentumFeature` (the four legacy factors, re-expressed through the new
interface with byte-identical binary behavior), `ATRDonchianSqueezeFeature`,
`OBVFeature`, `CrossSectionalReturnRankFeature`, `SharedSeriesFeature`, and
`PerAssetExternalSeriesFeature`. `FEATURE_REGISTRY` holds the four legacy
features; `FEATURE_REGISTRY_WITH_ATR`, `FEATURE_REGISTRY_WITH_OBV`, and
`FEATURE_REGISTRY_WITH_ATR_OBV` are additive variants. The legacy
`conviction()`/`conviction_dyn()` scoring functions do not consume the
registry; they are a separate, hand-coded 4-factor scorer used by `run`
and `run_dynamic`.

## Validation Framework

See `03_Validation.md` for full detail. Summary of the functions
involved: `compute_fold_boundaries` (expanding-window fold construction),
`run_validate`/`run_validate_dynamic` (multi-fold validation of a fixed
configuration), `run_sweep` (in-sample/out-of-sample parameter grid
search), `build_training_samples`/`simulate_trade_outcome` (label
generation using the same triple-barrier mechanics as the live engine),
`purge_and_embargo_samples`/`compute_label_uniqueness` (Lopez de Prado
purged walk-forward methodology), `logistic_regression_fit`/
`bootstrap_logreg` (linear model fitting with bootstrap confidence
intervals), `run_probability_diagnostic` (ROC AUC, PR AUC, Brier score,
calibration), `run_nonlinear_benchmark` (XGBoost/LightGBM/RandomForest
comparison against the linear baseline), `run_attribution`/
`permutation_test`/`information_gain`/`phi_coefficient` (feature
attribution), `verify_causality`/`verify_label_fidelity` (leakage
audits), and `run_monte_carlo_robustness`/`compute_risk_of_ruin`
(portfolio-level bootstrap robustness).

## Portfolio Engine

See `04_Portfolio_Engine.md` for full detail. `aggregate_positions()`
collapses per-leg trade records into completed positions for reporting.
`build_portfolio_equity_curve()` compounds a chronological, per-leg
cashflow sequence into an equity curve, with correlation-aware position
sizing (`compute_position_weight`, `compute_portfolio_correlation`) and
an optional heat cap. `compute_portfolio_metrics()` derives CAGR, Sharpe,
Sortino, Calmar, Max Drawdown, Profit Factor, and Recovery Factor from
that curve. `run_monte_carlo_robustness()`/`compute_risk_of_ruin()`
provide bootstrap-based robustness and ruin-probability estimates.

## Risk Engine

Risk is expressed at two levels: per-trade (the stop-distance-scaled
R-multiple computed by the entry/exit engine, corrected for transaction
costs per C1) and portfolio-level (position-weight scaling via
`compute_position_weight`, correlation-aware weight reduction via C4, and
an optional heat cap in `build_portfolio_equity_curve`). `RISK_PROFILES`
defines three named configurations -- GROWTH, BALANCED,
CAPITAL_PRESERVATION -- each specifying `risk_pct_per_trade`,
`max_positions`, `sizing_mode`, `heat_cap`, and a `ruin_threshold`.

## Data Flow

```
CoinGecko API / CSV files / synthetic generator
        -> load_csv_history / load_csv_hilo / fetch_history / synthetic_history
        -> compute_data_integrity_flags (C6)
        -> run / run_dynamic / run_dynamic_attr (entry/exit engine)
        -> trades (per-leg records, R-multiple already cost-corrected per C1)
        -> aggregate_positions (reporting) | build_portfolio_equity_curve (portfolio metrics)
        -> report() / compute_portfolio_metrics() / validation and audit functions
        -> CLI stdout
```

## Module Relationships

All functions live in one module (`turtle_backtest.py`); there is no
package hierarchy. Relationships are by direct function call:
`main()` parses CLI flags and dispatches to one of the report/validate/
audit/attribute/portfolio functions, each of which calls into the shared
engines (`run`/`run_dynamic`/`run_dynamic_attr`), the shared feature
functions (`conviction`/`conviction_dyn`/`conviction_components`/
`conviction_components_v2`), and the shared statistical helpers
(`logistic_regression_fit`, `linear_regression_fit`, `pearson_r`,
`roc_auc`, etc.). `aggregate_positions()` and `build_portfolio_equity_curve()`
are consumed by `report()`, `summarize()`, `compute_portfolio_metrics()`,
and (through that) every portfolio-level reporting and Monte Carlo
function.

## Design Decisions

- **Single-file design.** Documented project decision: the entire engine
  is one script rather than a package, to keep every code path visible
  in one place during the frozen-architecture review process.
- **Three parallel trading engines rather than one.** `run`,
  `run_dynamic`, and `run_dynamic_attr` duplicate the same entry/exit/
  stop/target logic. This duplication was preserved rather than
  refactored during the C1-C6 correction process, per the explicit
  "architecture frozen, no redesign" constraint each correction operated
  under; each correction (C1, C2) was applied identically to all three
  engines to keep them behaviorally consistent.
- **Frozen entry/exit logic with additive gates.** Corrections C2, C5
  (rejected variant), and C6 were implemented as additional day-skip
  conditions parallel to gates that already existed (missing-data skip,
  warmup gate, regime gate), rather than as changes to the existing
  gates themselves.
- **Reporting/accounting separated from equity computation (C3).**
  `aggregate_positions()` is used only for display statistics
  (win rate, expectancy, per-asset breakdown); `build_portfolio_equity_curve()`
  operates on the raw chronological per-leg cashflow list. This
  separation exists specifically because the two have different
  correctness requirements: position-level counting for reporting,
  strict chronological cashflow timing for equity/risk metrics.
- **Point-in-time constraints applied without an external database
  (C4, and the accepted portion of C5).** Correlation and liquidity
  measures are computed from data already loaded by the engine
  (historical OHLCV), using only information available before each
  decision date, rather than introducing a point-in-time universe or
  market-cap database.

## What Is Intentionally NOT Included

- A live execution/order-routing layer. This engine is a backtester and
  research tool only; it does not connect to any exchange, broker, or
  live data feed for trading purposes.
- A point-in-time universe/listing database. Universe composition is
  fixed by whichever CSV files or watchlist the user supplies.
- Any strategy logic beyond the frozen entry/exit/conviction rules
  documented in `02_Strategy.md`.
- Position reconciliation against external/broker state. The
  data-integrity gate (C6) prevents new decisions on bad data within the
  backtester; it does not, and cannot, reconcile internal position state
  against a live venue (see `08_Known_Limitations.md`).
