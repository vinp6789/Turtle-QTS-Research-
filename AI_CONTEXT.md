# AI_CONTEXT.md

Engineering handoff document for any future AI assistant (Claude,
ChatGPT, Codex, or other) picking up work related to this project. Read
this before touching anything. Turtle QTS Research Engine is permanently
frozen v1.0. This document orients you; it does not authorize changes.

---

## 1. Project Purpose

Turtle QTS Research Engine (`turtle_backtest.py`) is a single-file Python
backtesting and research platform for a crypto swing-trading strategy. It
exists to answer one question rigorously: does the Turtle QTS entry/exit
logic have a real, statistically defensible edge net of realistic costs,
validated by institution-grade methodology (purged/embargoed walk-forward
CV, bootstrap, Monte Carlo, permutation testing)? It is not a live
trading system. It has no order execution, no broker integration, and no
custody logic.

## 2. High-Level Architecture

```
Data Ingestion -> Data Integrity Gate (C6) -> Regime Filter ->
Conviction/Feature Engine -> Entry/Exit Engine (frozen) ->
Position Accounting -> [Validation Layer | Portfolio Engine] -> Reporting
```

Three parallel trading engines share identical entry/exit/stop/target
logic: `run` (legacy, shared-index, used by `--synthetic`/`--api-key`),
`run_dynamic` (dynamic-universe, default CSV report path),
`run_dynamic_attr` (attribution variant, used by validation/portfolio
commands). Full detail: `01_Architecture.md`.

## 3. Core Modules

There is no package structure -- one file, top-level functions/classes.
Responsibility groups (see `12_DEVELOPER_GUIDE.md` section 2 for the
complete list):

- Data loading: `fetch_history`, `load_csv_history`, `load_csv_hilo`,
  `synthetic_history`, `compute_data_integrity_flags`.
- Feature framework: `ConvictionFeature` and subclasses,
  `FEATURE_REGISTRY` and variants, `conviction`/`conviction_dyn`
  (legacy), `conviction_components`/`conviction_components_v2`
  (generalized).
- Trading engines: `run`, `run_dynamic`, `run_dynamic_attr`.
- Validation: purge/embargo, bootstrap, Monte Carlo, permutation,
  attribution -- see `03_Validation.md`.
- Portfolio: `build_portfolio_equity_curve`, `compute_portfolio_metrics`,
  `compute_position_weight`, `RISK_PROFILES`.
- Reporting: `aggregate_positions`, `report`, `summarize`.
- CLI: `main`.

## 4. Data Flow

```
raw OHLCV -> per-asset (dates, closes, vols[, highs, lows])
  -> compute_data_integrity_flags (once per asset)              [C6]
  -> per-day loop: regime check -> warmup gate -> integrity gate
     -> exit check (intrabar H/L) -> entry candidacy -> conviction score
     -> position-cap-limited admission
  -> trades: per-leg dicts, r already cost-corrected in R-units  [C1]
  -> aggregate_positions(trades) -> report()/summarize()          [C3]
  -> build_portfolio_equity_curve(trades) -> compute_portfolio_metrics()
     correlation-aware weight [C4], optional heat cap
  -> run_monte_carlo_robustness -> compute_risk_of_ruin
  -> run_survivorship_dependence_report (diagnostic only)         [C5]
```

## 5. Frozen Architecture

Locked: the dynamic-universe date-aligned engine, the pluggable feature
registry, the purged/embargoed walk-forward validation stack, the
label/uniqueness auditing, and the portfolio/risk layer. No structural
redesign occurred during the C1-C6 correction process; every correction
was implemented as an addition (a new gate, a new aggregation step, a
corrected formula) inside the existing structure. Full detail:
`07_Architecture_Freeze.md`.

## 6. Frozen Strategy

Entry: BTC regime not RISK_OFF (close more than 0.5% below its 200D EMA)
AND conviction score >= `CONV_THRESHOLD` (default 70). Conviction: base
30 + relative strength (+25, 30D return beats BTC) + trend (+20, close
above 50D EMA) + volume expansion (+15, 7D avg volume beats prior 23D avg
by >15%) + momentum (+10, 30D return >10%), capped at 100. Exit
precedence: stop (Low-based, C2) > T1 partial at 1.5R (High-based, half
position, stop trails to breakeven) > T2 at 3R (High-based, remainder) >
signal loss (regime flip or conviction < threshold - 10, close-based).
Full detail: `02_Strategy.md`.

## 7. Frozen Research Conclusions

The six-feature OHLCV-derived family (relative strength, trend, volume
expansion, momentum, ATR/Donchian squeeze, OBV) shows no statistically
meaningful predictive information for forward returns (pooled |r|
approximately 0.05-0.08, joint R^2 approximately 1.3%, meta-label AUC
approximately 0.4837, below 0.50). A nonlinear model (XGBoost/LightGBM/
RandomForest) tested against the same frozen feature set did not clear
the project's own materiality bar (R^2 > 0.03 and more than double the
linear baseline) -- the information ceiling is in the feature set, not
model expressiveness. Further OHLCV-derived indicator engineering on
this family is permanently rejected. Research is redirected toward
orthogonal sources (Open Interest -- highest priority, funding rates,
on-chain metrics, macro liquidity) which remain untested future work; the
infrastructure to consume them (`SharedSeriesFeature`,
`PerAssetExternalSeriesFeature`) already exists and is frozen. Full
detail: `05_Research_Findings.md`.

## 8. Validation Methodology

Walk-forward (`compute_fold_boundaries`, `run_validate`/
`run_validate_dynamic`, `run_sweep`), purged validation and embargo
(`run_purged_validation`, `purge_and_embargo_samples`, Lopez de Prado
methodology), bootstrap (`bootstrap_logreg`), Monte Carlo
(`run_monte_carlo_robustness`, `compute_risk_of_ruin`), permutation
testing (`permutation_test`), feature attribution (`run_attribution`,
`information_gain`, `phi_coefficient`), regime testing (BTC bull/bear
stratification inside `run_validate`), portfolio testing
(`run_portfolio_report`, `run_risk_profile_comparison`). Full detail:
`03_Validation.md`.

## 9. Portfolio Methodology

`build_portfolio_equity_curve` compounds a chronological, per-leg
cashflow sequence (never aggregated -- see C3). Sizing via
`compute_position_weight` (`fixed`/`vol_targeted`/`conviction_weighted`,
clamped 0.3x-2.0x), correlation-aware weight reduction (C4, point-in-time
expanding window, threshold 0.5, divisor `1 + count of correlated open
positions`), optional heat cap. `RISK_PROFILES`: GROWTH (2.0%/trade, cap
4, conviction_weighted, 10% heat), BALANCED (1.0%, cap 3, fixed, 5%
heat), CAPITAL_PRESERVATION (0.5%, cap 2, fixed, 2% heat).
`compute_portfolio_metrics` derives CAGR, Sharpe, Sortino, Max Drawdown,
Calmar, Profit Factor, Recovery Factor. Full detail:
`04_Portfolio_Engine.md`.

## 10. C1-C6 Summary

| # | Correction | Final state |
|---|---|---|
| C1 | Transaction cost: raw fraction subtracted instead of converted to R-units | Fixed at 14 sites: `(FEE+SLIP)/stop_d`. Frozen. |
| C2 | Close-only exits missed intrabar stop/target touches | Intrabar High/Low added, stop takes precedence on conflict, close-only fallback preserved exactly. Frozen. |
| C3 | T1-half counted as an independent trade; equity curve v1 fix collapsed cashflow timing | Reporting uses `aggregate_positions` (position-level); equity curve reverted to raw chronological per-leg cashflows (no aggregation). Frozen. |
| C4 | No correlation adjustment; v1 fix used a static full-sample (look-ahead) correlation matrix | Point-in-time expanding-window correlation, computed from only already-closed trades. Frozen. |
| C5 | Hand-picked survivor universe; v1 "fix" was an undisclosed liquidity-filter strategy redesign, rejected | Reverted to a read-only diagnostic (`run_survivorship_dependence_report`) outside the engine; trade list identical to C4. Frozen. |
| C6 | No data-integrity check; live position state has no fail-safe | `compute_data_integrity_flags` blocks new entries/preserves stops on bad data (backtest scope verified). Live-execution reconciliation explicitly unresolved and out of scope. Frozen (backtest scope). |

Full detail: `06_C1_C6_Corrections.md`, chronology in `10_CHANGELOG.md`.

## 11. Known Limitations

Survivorship bias in universe *selection* (not within-universe liquidity,
which C5's diagnostic quantifies) persists and requires a point-in-time
listing database this project does not have. No exchange-outage,
network-partition, or custody-failure protection exists (no live
connection exists at all). No live execution. No capacity/market-impact
modeling. Results depend entirely on supplied data quality beyond what
C6's per-asset integrity check can detect. Full detail:
`08_Known_Limitations.md`.

## 12. Stable Interfaces

Treat these as the frozen contract for any consumer (documentation,
future projects, or an AI resuming work):

- **`trades` record schema:** `sym`, `r`, `pct`, `reason`, `conv`,
  `entry_day`, `exit_day`, `entry_px`, `exit_px`, `stop_d`.
- **`compute_portfolio_metrics()` return schema:** `n_trades`, `years`,
  `trades_per_year`, `final_equity`, `cagr`, `sharpe`, `sortino`,
  `max_dd_pct`, `calmar`, `profit_factor`, `recovery_factor`, `curve`,
  `min_equity_frac`.
- **`RISK_PROFILES` schema:** `risk_pct_per_trade`, `max_positions`,
  `sizing_mode`, `heat_cap`, `ruin_threshold`.

## 13. Future Projects

**Execution Engine.** Separate project. Would consume this engine's
validated strategy/risk-profile parameters to place live orders. Must
independently design position-state reconciliation against broker
reality -- the C6 final verification found no such mechanism exists here
and none should be retrofitted into the Research Engine.

**Stock Research Engine.** Separate project. Would apply the same
validation methodology to equities but needs its own data ingestion and
market-microstructure assumptions (session gaps, halts, corporate
actions -- none modeled here). Must not import or modify
`turtle_backtest.py`.

**Portfolio Intelligence Layer.** Documented project decision: referenced
in prior project history as a related, separate system (multi-layer
macro/conviction/allocation pipeline) rather than a component of this
engine. Any such layer should consume this engine's stable interfaces
(section 12) rather than depend on internal engine control flow, and must
not modify this source.

## 14. Things an AI Must NEVER Change Inside Turtle QTS Research Engine

- Trading strategy (entry/exit/stop/target logic in `run`/`run_dynamic`/
  `run_dynamic_attr`).
- Indicators (the four legacy `ConvictionFeature` weights/formulas:
  `RelativeStrengthFeature`, `TrendFeature`, `VolumeExpansionFeature`,
  `MomentumFeature`).
- Validation methodology (fold construction, purge/embargo, bootstrap
  mechanics).
- Feature framework (`FEATURE_REGISTRY` base set; extension by addition
  only).
- Portfolio framework (`build_portfolio_equity_curve`,
  `compute_portfolio_metrics`, `RISK_PROFILES` fixed parameters).
- Architecture (the three-engine structure, the single-file design, the
  frozen data-flow order).

The only sanctioned exception: a new, critical defect meeting the same
bar as C1-C6 (causes incorrect live trading, capital loss, or invalid
research conclusions), corrected via the same propose-implement-audit-
freeze process documented in `06_C1_C6_Corrections.md`, and only after
explicit authorization -- never inferred or self-initiated.

## 15. Recommended Reading Order

1. `README.md` -- orientation.
2. `01_Architecture.md` -- structure.
3. `02_Strategy.md` -- what the strategy actually does.
4. `05_Research_Findings.md` -- what was concluded and why further OHLCV
   work is closed.
5. `06_C1_C6_Corrections.md` -- what was fixed and how each fix was
   verified.
6. `07_Architecture_Freeze.md` -- what is and is not permitted going
   forward.
7. `04_Portfolio_Engine.md` and `03_Validation.md` -- as needed for
   portfolio or validation work.
8. `11_CLI_REFERENCE.md` and `12_DEVELOPER_GUIDE.md` -- as needed to run
   or navigate the code.
9. `08_Known_Limitations.md` and `09_Lessons_Learned.md` -- context
   before proposing any new work in an adjacent project.

## 16. Approximate Project Timeline

Documented project decision (exact calendar dates are not verifiable
from the source and are not asserted here): the project proceeded
through an initial research and development phase (strategy
construction, feature framework, validation tooling, portfolio engine --
see `09_Lessons_Learned.md` and the "Earlier context"/"Recent months"
history referenced in prior project sessions), followed by a formal
architecture gate review, followed by the sequential C1 through C6
correction process (each implemented, audited, and frozen in order, with
C3, C4, and C5 each undergoing a rejected or revised first
implementation before being frozen -- see `10_CHANGELOG.md`), followed by
the formal architecture freeze declaration, followed by generation of
this documentation set.

## 17. Glossary of Project Terminology

- **Conviction score** -- the 0-100 multi-factor score (legacy or
  feature-registry) gating entry eligibility.
- **Regime / RISK_OFF** -- BTC's close more than 0.5% below its own 200D
  EMA; entries are blocked universe-wide when true.
- **R-multiple** -- a trade's return expressed as a multiple of its own
  stop distance (1R = the amount risked to the stop).
- **Stop distance / stop_d** -- the volatility-derived distance from
  entry to stop, defining 1R.
- **T1 / T2** -- profit targets at 1.5R and 3R respectively; T1 triggers
  a half-position partial exit and a stop trail to breakeven.
- **Leg** -- one trade record as generated by the engine; a position that
  hits T1 produces two legs (`t1_half` and the eventual closing leg).
- **Position** -- the aggregation of all legs sharing one `(sym,
  entry_day)` key, via `aggregate_positions()`; used for reporting only.
- **Purge / Embargo** -- Lopez de Prado walk-forward CV controls removing
  training samples whose label window overlaps a test fold (purge) and
  enforcing a gap after each fold before training resumes (embargo).
- **Heat cap** -- an optional ceiling on total concurrent portfolio risk
  across all open positions.
- **Risk of Ruin** -- the fraction of Monte Carlo-resampled equity paths
  that ever fall to or below a profile's `ruin_threshold`.
- **Point-in-time** -- a calculation using only information that would
  have been available at the historical moment of the decision, with no
  future data (the property C4's revision restored and C6 relies on).
- **Frozen** -- not to be modified except via the C1-C6-style critical-
  defect-correction process with explicit authorization.

## 18. Frequently Referenced Functions and Where They Live

All functions are in `turtle_backtest.py` (single file, no package
paths). Quick-reference by task:

| Task | Function(s) |
|---|---|
| Load CSV OHLCV | `load_csv_history`, `load_csv_hilo` |
| Score conviction | `conviction`, `conviction_dyn`, `conviction_components`, `conviction_components_v2` |
| Run a backtest | `run`, `run_dynamic`, `run_dynamic_attr` |
| Generate labels for ML/validation | `simulate_trade_outcome`, `build_training_samples` |
| Fold construction | `compute_fold_boundaries` |
| Purge/embargo | `purge_and_embargo_samples`, `compute_label_uniqueness` |
| Fit linear model | `logistic_regression_fit`, `bootstrap_logreg` |
| Feature attribution | `run_attribution`, `permutation_test`, `information_gain` |
| Aggregate trades into positions (reporting only) | `aggregate_positions` |
| Build compounded equity curve | `build_portfolio_equity_curve` |
| Compute portfolio metrics | `compute_portfolio_metrics` |
| Position sizing weight | `compute_position_weight` |
| Correlation matrix | `compute_portfolio_correlation` |
| Monte Carlo robustness | `run_monte_carlo_robustness`, `compute_risk_of_ruin` |
| Survivorship diagnostic | `run_survivorship_dependence_report` |
| Data-integrity check | `compute_data_integrity_flags` |
| CLI entry point | `main` |
