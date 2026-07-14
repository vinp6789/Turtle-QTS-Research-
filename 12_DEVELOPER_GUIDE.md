# 12. Developer Guide

This document is for developers who need to read, understand, or safely
extend the Turtle QTS Research Engine codebase. It describes the frozen
v1.0 source as it exists, not proposed changes. See
`07_Architecture_Freeze.md` for what may and may not be modified.

## 1. Overall Code Organization

`turtle_backtest.py` is a single Python module, standard-library-only
except for the optional `--nonlinear-benchmark` path. It has no package
structure; every function and class is a top-level module member. Code
is organized top-to-bottom roughly in the order a request flows through
the system: constants and data loading first, indicators and the feature
framework next, the trading engines, then label generation and
validation/audit functions, then the portfolio engine, and `main()`/CLI
argument parsing last.

## 2. Major Functions Grouped by Responsibility

**Data loading:** `fetch_history` (CoinGecko API), `load_csv_history`,
`load_csv_hilo`, `audit_csv_raw`, `run_data_audit`, `synthetic_history`,
`load_shared_series_from_csv`, `load_shared_series_from_coingecko`,
`load_per_asset_series_from_csvs`, `build_per_asset_series_lookup`,
`compute_data_integrity_flags` (C6).

**Indicators:** `ema`, `compute_rsi`, `rolling_ema_series`,
`trailing_return`.

**Feature framework:** `conviction`, `conviction_dyn` (legacy scorer);
`ConvictionFeature` and its subclasses (`RelativeStrengthFeature`,
`TrendFeature`, `VolumeExpansionFeature`, `MomentumFeature`,
`ATRDonchianSqueezeFeature`, `OBVFeature`,
`CrossSectionalReturnRankFeature`, `SharedSeriesFeature`,
`PerAssetExternalSeriesFeature`); `FEATURE_REGISTRY` and its variants;
`conviction_components`, `conviction_components_v2`;
`precompute_cross_sectional_ranks`, `compute_btc_volume_dominance_series`.

**Trading engines:** `run`, `run_dynamic`, `run_dynamic_attr`.

**Label generation (used by validation, not by the live engines):**
`simulate_trade_outcome`, `simulate_trade_outcome_diagnostic`,
`build_training_samples`, `build_label_audit_samples`.

**Validation:** `compute_fold_boundaries`, `run_validate`,
`run_validate_dynamic`, `run_sweep`, `purge_and_embargo_samples`,
`compute_label_uniqueness`, `verify_causality`, `verify_label_fidelity`,
`run_label_audit`, `run_feature_warmup_audit`, `find_first_valid_index`,
`standardize`, `logistic_regression_fit`, `bootstrap_logreg`,
`percentile`, `run_train_model`, `run_purged_validation`,
`sigmoid`, `roc_auc`, `pr_auc`, `brier_score`, `calibration_curve`,
`run_probability_diagnostic`, `run_candidate_only_comparison`,
`run_feature_ablation_experiment`, `linear_regression_fit`,
`out_of_sample_r2`, `mae`, `rmse`, `run_nonlinear_benchmark`,
`spearman_r`, `run_forward_return_audit`, `entropy`, `information_gain`,
`permutation_test`, `phi_coefficient`, `pearson_r`, `run_attribution`,
`run_rs_lookback_sweep`, `diagnose_component_timing`.

**Portfolio:** `compute_max_drawdown`, `compute_position_weight`,
`build_portfolio_equity_curve`, `compute_portfolio_metrics`,
`RISK_PROFILES`, `search_growth_profile_parameters`,
`run_portfolio_report`, `run_monte_carlo_robustness`,
`compute_risk_of_ruin`, `print_monte_carlo_report`,
`compute_portfolio_correlation`, `print_correlation_report`,
`run_survivorship_dependence_report` (C5), `run_risk_profile_comparison`,
`run_autonomous_portfolio_optimization`.

**Reporting:** `aggregate_positions` (C3), `report`, `summarize`.

**CLI:** `main`.

## 3. Call Flow From `main()` to Every Major Subsystem

```
main()
  parse CLI flags
  apply watchlist overrides (--watchlist / --add / --remove / --conv-threshold)
  |
  +-- if --audit-data: run_data_audit()                          [return]
  |
  +-- load data:
  |     --csv-dir -> load_csv_history / load_csv_hilo per matched file
  |     --api-key -> fetch_history per token
  |     --synthetic -> synthetic_history per token
  |
  +-- (CSV mode) build active feature registry from
  |     --use-atr / --use-obv / --use-xsrank / --use-shared-series /
  |     --use-per-asset-series
  |
  +-- dispatch on mode flag (CSV mode):
  |     --sweep              -> run_sweep()
  |     --validate           -> run_validate_dynamic()
  |     --train-model        -> run_train_model()
  |     --audit-warmup       -> run_feature_warmup_audit()
  |     --audit-labels       -> run_label_audit()
  |     --purged-validation  -> run_purged_validation()
  |     --audit-forward-return -> run_forward_return_audit()
  |     --risk-profiles      -> run_risk_profile_comparison()
  |     --optimize-portfolio -> run_autonomous_portfolio_optimization()
  |     --portfolio-report   -> run_portfolio_report()
  |     --nonlinear-benchmark -> run_nonlinear_benchmark()
  |     --audit-probability (+ --candidate-only + --exclude-feature)
  |                          -> run_feature_ablation_experiment() /
  |                             run_candidate_only_comparison() /
  |                             run_probability_diagnostic()
  |     --attribute          -> run_attribution()
  |     --rs-lookback-sweep  -> run_rs_lookback_sweep()
  |     --diagnose-component -> diagnose_component_timing()
  |     (none of the above)  -> report(run_dynamic(...))            [default]
  |
  +-- dispatch (non-CSV mode):
        --sweep    -> run_sweep()
        --validate -> run_validate()
        (default)  -> report(run(...))
```

Every mode-specific function above calls into one or more of `run`,
`run_dynamic`, or `run_dynamic_attr` internally (directly, or via
`simulate_trade_outcome`/`build_training_samples` for label generation),
and into the shared statistical helpers listed in section 2.

## 4. Data Flow Through the System

```
raw OHLCV (API / CSV / synthetic)
  -> per-asset (dates, closes, vols) and optional (dates, highs, lows)
  -> compute_data_integrity_flags (per asset, once)                [C6]
  -> per-day loop in run / run_dynamic / run_dynamic_attr:
       regime check (BTC 200D EMA)
       per-asset: warmup gate -> integrity gate [C6] -> exit check [C2]
                  -> entry-candidate collection -> conviction scoring
       position-cap-limited entry admission
  -> trades: list of per-leg dicts (sym, r, pct, reason, conv,
             entry_day, exit_day, entry_px, exit_px, stop_d, ...)
             r already cost-corrected in R-units [C1]
  -> aggregate_positions(trades)          -> report() / summarize()  [C3]
  -> build_portfolio_equity_curve(trades) -> compute_portfolio_metrics()
       correlation-aware weight [C4], optional heat cap
  -> run_monte_carlo_robustness(trades)   -> compute_risk_of_ruin()
  -> run_survivorship_dependence_report(trades)  [C5, diagnostic only]
```

## 5. Where Each Major Structure Is Created and Consumed

- **`histories` / `histories_dated`** (`{cid: (dates, closes, vols)}` or
  `{cid: (closes, vols)}`): created in `main()`'s data-loading branch;
  consumed by every trading engine, validation function, and audit
  function that takes `histories`/`histories_dated` as its first
  argument.
- **`hilo_dated`** (`{cid: (dates, highs, lows)}`): created in `main()`
  only when `--use-atr` is set (loaded via `load_csv_hilo`); consumed by
  `run`, `run_dynamic`, `run_dynamic_attr` (C2 exit detection) and by
  `ATRDonchianSqueezeFeature` (feature computation).
- **`trades`** (list of per-leg dicts): created by `run`/`run_dynamic`/
  `run_dynamic_attr`; consumed by `report`, `summarize`,
  `aggregate_positions`, `build_portfolio_equity_curve`,
  `compute_portfolio_metrics`, `run_monte_carlo_robustness`,
  `compute_portfolio_correlation`, `run_survivorship_dependence_report`.
- **`FEATURE_REGISTRY` and variants**: module-level constants, built once
  at import time; the active registry for a given run is selected in
  `main()` based on `--use-atr`/`--use-obv`/etc. and passed as the
  `registry` argument to `run_dynamic_attr` and every attribution/audit
  function.
- **`RISK_PROFILES`**: module-level constant dict; read by
  `run_risk_profile_comparison` and `search_growth_profile_parameters`.
  Not mutated at runtime except by `search_growth_profile_parameters`'s
  documented one-time GROWTH-profile parameter correction, itself part of
  the frozen source.
- **`TOKENS`**: module-level dict, mutated at runtime by
  `--watchlist`/`--add`/`--remove` and by CSV-mode symbol matching in
  `main()`. Read by every engine and reporting function via
  `TOKENS.get(cid, cid)`.

## 6. Extension Points

The source was designed with the following additive extension points,
consistent with the architecture freeze (extension, not modification, of
the frozen structures):

- **New `ConvictionFeature` subclasses**, added to a new registry dict
  built from `dict(FEATURE_REGISTRY, new_feature=...)`, following the
  existing pattern used by `FEATURE_REGISTRY_WITH_ATR` etc. Must not
  modify `FEATURE_REGISTRY` itself or any existing feature class.
- **New per-asset or shared external data series**, via
  `PerAssetExternalSeriesFeature`/`SharedSeriesFeature` and their
  existing CSV-loading functions (`load_per_asset_series_from_csvs`,
  `load_shared_series_from_csv`). This is the intended path for the
  future-work items in `05_Research_Findings.md` (Open Interest, funding,
  on-chain, macro liquidity).
- **New CLI flags that route to new, additive report/audit functions**,
  following the existing `main()` dispatch pattern, provided they do not
  alter the behavior of any existing flag or default code path.

## 7. Areas Intentionally Frozen

Per `07_Architecture_Freeze.md`: the three trading engines' entry/exit/
stop/target/position-cap logic; the legacy and generalized conviction
scorers and the four legacy `ConvictionFeature` classes; the C1 cost
correction; the C2 intrabar exit logic; `aggregate_positions` and
`build_portfolio_equity_curve`'s chronological-cashflow accounting (C3);
the C4 point-in-time correlation-aware sizing; `RISK_PROFILES`; the
entire validation methodology listed in `03_Validation.md`; the C6
data-integrity gate.

## 8. Safe Places for Future Development

Consistent with section 6 and the architecture freeze: new additive
`ConvictionFeature` subclasses and their registries; new external data
series loaders following the existing `SharedSeriesFeature`/
`PerAssetExternalSeriesFeature` pattern; new read-only diagnostic/report
functions that consume an already-generated `trades` list without
altering it (following the `run_survivorship_dependence_report` pattern
from C5); a future, separate live-execution project that consumes this
engine's outputs without modifying this source; a future, separate
Stock Research Engine project that does not import or modify this
source.

## 9. Areas That Must Never Be Modified Inside the Research Engine

Per `07_Architecture_Freeze.md`, restated here for developer reference:
trading strategy, indicators, validation methodology, feature framework,
portfolio framework, and architecture. Concretely, this means: no change
to entry/exit/stop/target logic in `run`/`run_dynamic`/`run_dynamic_attr`;
no change to the weights or formulas of `RelativeStrengthFeature`,
`TrendFeature`, `VolumeExpansionFeature`, `MomentumFeature`; no change to
fold-construction, purge/embargo, or bootstrap methodology; no change to
`aggregate_positions`/`build_portfolio_equity_curve`'s accounting
mechanics beyond a correction meeting the same critical-defect bar as
C1-C6; no change to `RISK_PROFILES`'s fixed parameters.

## 10. Relationship Between Research Engine, Future Execution Engine, and Future Stock Research Engine

**Research Engine (this project, `turtle_backtest.py`):** produces
validated backtest results, portfolio metrics, and research conclusions.
Contains no live order placement, no broker/exchange trading integration,
and no position-reconciliation mechanism (see C6 in
`06_C1_C6_Corrections.md` and `08_Known_Limitations.md`). Its outputs
(trade lists, portfolio metrics, validated feature set, risk profiles)
are the intended interface for any consumer.

**Future Execution Engine (separate project, not present in this
source):** would consume the Research Engine's validated strategy
parameters and risk profiles to place and manage live orders. Per the
C6 final verification, it would need its own state-reconciliation design
(broker-state vs. internal-state divergence during data outages) that
does not exist anywhere in this source and is explicitly out of scope
for the Research Engine to provide.

**Future Stock Research Engine (separate project, not present in this
source):** would apply comparable research methodology
(`03_Validation.md`) to equities, but would require its own data
ingestion, its own market-microstructure assumptions (session gaps,
trading halts, corporate actions -- none of which this engine models),
and its own frozen architecture. It would not import or modify
`turtle_backtest.py`.

**Stable interfaces.** The following are expected to remain stable as
integration points for either future project, since they are the frozen,
audited output surface of this engine: the `trades` per-leg record
schema (`sym`, `r`, `pct`, `reason`, `conv`, `entry_day`, `exit_day`,
`entry_px`, `exit_px`, `stop_d`), the `compute_portfolio_metrics()`
return-dict schema (`n_trades`, `years`, `trades_per_year`,
`final_equity`, `cagr`, `sharpe`, `sortino`, `max_dd_pct`, `calmar`,
`profit_factor`, `recovery_factor`, `curve`, `min_equity_frac`), and the
`RISK_PROFILES` schema (`risk_pct_per_trade`, `max_positions`,
`sizing_mode`, `heat_cap`, `ruin_threshold`). Any future project should
treat these as the stable contract rather than depending on internal
engine control flow.
