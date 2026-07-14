# 07. Architecture Freeze

## Architecture Frozen

The Turtle QTS Research Engine architecture is frozen. The dynamic-
universe date-aligned engine, the pluggable feature registry, the purged/
embargoed walk-forward validation stack, the label/uniqueness auditing,
and the portfolio/risk layer are locked in their current structural form.
The six approved critical corrections (C1 through C6, see
`06_C1_C6_Corrections.md`) have been implemented, mathematically audited,
and declared frozen.

## Frozen Components

- **Entry/exit engines** -- `run`, `run_dynamic`, `run_dynamic_attr`, and
  their shared entry-gating, exit-precedence, stop/target, and
  position-cap logic, including the C2 intrabar High/Low correction.
- **Conviction scoring** -- the legacy 4-factor scorecard
  (`conviction`/`conviction_dyn`) and the generalized feature-registry
  scorer (`conviction_components`/`conviction_components_v2`), including
  `RelativeStrengthFeature`, `TrendFeature`, `VolumeExpansionFeature`,
  `MomentumFeature`, `ATRDonchianSqueezeFeature`, `OBVFeature`,
  `CrossSectionalReturnRankFeature`, `SharedSeriesFeature`, and
  `PerAssetExternalSeriesFeature`.
- **Transaction cost accounting** -- the C1-corrected R-unit cost
  conversion, applied uniformly across all engines and label simulators.
- **Position and portfolio accounting** -- `aggregate_positions()`
  (reporting), `build_portfolio_equity_curve()` and
  `compute_portfolio_metrics()` (chronological per-leg cashflow
  accounting, per C3), `compute_position_weight()`, `RISK_PROFILES`, and
  the C4 point-in-time correlation-aware sizing adjustment.
- **Validation methodology** -- purged/embargoed walk-forward
  cross-validation, bootstrap confidence intervals, Monte Carlo
  robustness testing, permutation testing, feature attribution, and every
  audit function documented in `03_Validation.md`.
- **Data-integrity gate** -- `compute_data_integrity_flags()` and its
  wiring into `run_dynamic`/`run_dynamic_attr` (C6, backtest scope).
- **Survivorship diagnostic** -- `run_survivorship_dependence_report()`
  (C5, revised, read-only, outside the trading engine).

## Allowed Future Changes

Only the following categories of change are in scope going forward:

1. **Critical defect corrections only.** A newly discovered defect
   meeting the same bar as C1-C6 (causes incorrect live trading, capital
   loss, or invalid research conclusions) may be corrected inside the
   frozen architecture, following the same propose-implement-audit-freeze
   process used for C1-C6. This does not reopen the architecture itself
   unless the defect is structural.
2. **Live execution layer.** A separate project. The C6 final
   verification identified that live position-state reconciliation
   during a data outage is unresolved within this engine and requires a
   live-execution-specific design; that design is explicitly out of
   scope for Turtle QTS Research Engine.
3. **Stock Research Engine.** A separate project. This engine's data
   assumptions (24/7 markets, CoinGecko/CryptoDataDownload-format
   ingestion) and validated conclusions are crypto-specific and are not
   to be extended to equities within this codebase.

## Explicit Non-Modification Statement

The following will never be modified inside Turtle QTS Research Engine:

- Trading strategy
- Indicators
- Validation methodology
- Feature framework
- Portfolio framework
- Architecture

Any work falling into these categories belongs to a new, separate
project, not to a change against this frozen source.
