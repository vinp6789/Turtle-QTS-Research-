# Turtle QTS Research Engine

## Project Overview

Turtle QTS Research Engine is a single-file Python backtesting and research
platform (`turtle_backtest.py`) for a crypto swing-trading strategy
("Turtle QTS"). It replays the strategy's entry, exit, conviction-scoring,
and regime-filtering logic over historical daily OHLCV data, and provides a
research toolchain for validating whether the strategy has a genuine,
reproducible statistical edge net of realistic trading costs.

The engine is dependency-free for all core functionality (Python standard
library only: `argparse`, `json`, `math`, `random`, `sys`, `time`,
`datetime`, `urllib.request`). The optional `--nonlinear-benchmark` command
requires one of `xgboost`, `lightgbm`, or `scikit-learn` to be installed
separately; every other command has no external dependencies.

Documented project decision: the source file's own header comment
references `pip install requests`, but the shipped code uses only
`urllib.request` from the standard library. No `requests` dependency
exists in the current source.

## Objectives

- Determine whether the Turtle QTS entry/exit logic produces a real,
  statistically defensible edge, net of realistic transaction costs,
  rather than an artifact of an optimistic backtest.
- Validate that edge using institution-grade methodology: purged and
  embargoed walk-forward cross-validation, bootstrap confidence intervals,
  Monte Carlo robustness testing, and permutation significance testing.
- Identify and correct known accounting, execution, and risk-modeling
  defects (documented in `06_C1_C6_Corrections.md`) before any conclusion
  about live viability is drawn.
- Provide portfolio-level performance and risk reporting (CAGR, Sharpe,
  Sortino, Max Drawdown, Calmar, Profit Factor, Recovery Factor, Risk of
  Ruin) suitable for a go/no-go capital-deployment decision.

## Key Capabilities

- Data ingestion from CoinGecko (`--api-key`), local CSV files in
  CryptoDataDownload format (`--csv-dir`), or a deterministic synthetic
  self-test (`--synthetic`).
- A BTC-regime filter (200-day EMA, 0.5% deadband) gating all entries.
- Two conviction-scoring paths: a legacy 4-factor hand-set scorecard
  (`conviction`/`conviction_dyn`), and a generalized, pluggable feature
  framework (`conviction_components_v2`) supporting additional features
  (ATR/Donchian squeeze, OBV, cross-sectional return rank, shared/external
  series, per-asset external series).
- A dynamic-universe engine (`run_dynamic`, `run_dynamic_attr`) in which
  each asset keeps its own native-length history and enters the universe
  only once it has passed its own warmup, rather than truncating all
  assets to a shared start date.
- Purged and embargoed walk-forward cross-validation (Lopez de Prado
  methodology), label-uniqueness auditing, bootstrap coefficient
  confidence intervals, Monte Carlo portfolio robustness testing, and
  permutation significance testing.
- A portfolio engine computing compounded equity curves, CAGR, Sharpe,
  Sortino, Calmar, Max Drawdown, Profit Factor, Recovery Factor, and Risk
  of Ruin, with correlation-aware position sizing and an optional
  portfolio heat cap.
- A deterministic, point-in-time data-integrity check that blocks new
  trade entries on malformed or gapped market data (see C6 in
  `06_C1_C6_Corrections.md`).

## Folder Structure

Documented project decision: this project is delivered as a single Python
source file plus user-supplied data. No package/module directory
structure exists inside the source itself.

```
turtle_backtest.py       Single-file backtester and research CLI (source of truth)
<csv-dir>/                 User-supplied CryptoDataDownload-format CSVs, referenced via --csv-dir
                            Expected filename pattern: *SYMBOLUSDT*day*.csv (or similar; see --csv-dir help)
docs/                       This documentation set (README.md, 01-10)
```

The live n8n trading workflow referenced during development is a separate
artifact and is explicitly out of scope for this repository (see
`07_Architecture_Freeze.md`).

## Quick Start

```bash
# Engine self-test, no network or data files required
python turtle_backtest.py --synthetic

# Run against local CSV data (CryptoDataDownload format, requires a BTC file)
python turtle_backtest.py --csv-dir /path/to/csvs

# Full portfolio performance report on CSV data
python turtle_backtest.py --csv-dir /path/to/csvs --portfolio-report
```

## Main CLI Commands

| Command | Purpose |
|---|---|
| `--synthetic` | Deterministic engine self-test on synthetic random-walk data, no network. |
| `--csv-dir PATH` | Load historical data from local CSV files. |
| `--api-key KEY` | Fetch historical data from CoinGecko. |
| `--validate` | Multi-fold expanding-window validation of the current fixed configuration. |
| `--sweep` | Grid-search entry parameters with an in-sample/out-of-sample split. |
| `--attribute` | Feature attribution: ablation, correlation, permutation test, information gain. |
| `--train-model` | Fit an L2-regularized logistic regression per fold and compare to the hand-set scorecard. |
| `--purged-validation` | Purged and embargoed walk-forward cross-validation. |
| `--audit-data` | Read-only raw CSV integrity audit. |
| `--audit-warmup` | Feature warmup and causality/leakage audit. |
| `--audit-labels` | Label generation and fold-boundary leakage audit. |
| `--audit-probability` | Out-of-sample probability calibration audit (ROC AUC, PR AUC, Brier score). |
| `--audit-forward-return` | Tests raw linear/monotonic information content of features against forward return. |
| `--nonlinear-benchmark` | Compares a nonlinear model (XGBoost/LightGBM/RandomForest) against the linear baseline. |
| `--portfolio-report` | Full portfolio performance report from pooled out-of-sample trades. |
| `--risk-profiles` | Compares GROWTH, BALANCED, and CAPITAL_PRESERVATION sizing profiles. |
| `--optimize-portfolio` | Runs the full portfolio-construction diagnostic queue (Monte Carlo, correlation, sizing, sensitivity). |
| `--dump-trades` | Prints the full chronological trade log. |

Run `python turtle_backtest.py --help` for the complete flag reference
(approximately 50 flags covering watchlist control, feature-framework
toggles, and every audit/validation mode).

## High-Level Architecture (ASCII)

```
                        +---------------------------+
                        |        Data Ingestion       |
                        |  CoinGecko API / CSV /      |
                        |  synthetic self-test        |
                        +--------------+--------------+
                                       |
                                       v
                        +---------------------------+
                        |   Data Integrity Gate (C6)  |
                        |  point-in-time, per-asset    |
                        +--------------+--------------+
                                       |
                                       v
                        +---------------------------+
                        |   Regime Filter (BTC 200D EMA) |
                        +--------------+--------------+
                                       |
                                       v
                        +---------------------------+
                        |  Conviction / Feature Engine |
                        |  legacy scorecard  OR        |
                        |  pluggable feature registry   |
                        +--------------+--------------+
                                       |
                                       v
                        +---------------------------+
                        |  Entry / Exit Engine (frozen) |
                        |  run / run_dynamic /          |
                        |  run_dynamic_attr              |
                        |  intrabar High/Low exits (C2)  |
                        +--------------+--------------+
                                       |
                                       v
                        +---------------------------+
                        |  Position Accounting          |
                        |  per-leg R-multiples (C1)     |
                        |  position aggregation (C3)     |
                        +--------------+--------------+
                                       |
                     +-----------------+-----------------+
                     v                                   v
        +-------------------------+       +---------------------------------+
        |   Validation Layer       |       |   Portfolio Engine               |
        |  walk-forward, purged,   |       |  correlation-aware sizing (C4)   |
        |  bootstrap, Monte Carlo, |       |  heat cap, equity curve,          |
        |  permutation, attribution|       |  CAGR/Sharpe/Sortino/MaxDD/etc.  |
        +-------------------------+       +---------------------------------+
                     |                                   |
                     +-----------------+-----------------+
                                       v
                        +---------------------------+
                        |   Reporting (CLI output)    |
                        +---------------------------+
```

## Current Project Status: Frozen v1.0

The architecture and all six approved critical corrections (C1 through C6)
are frozen. No further changes to trading strategy, indicators, validation
methodology, feature framework, portfolio framework, or architecture are
in scope for this engine. See `07_Architecture_Freeze.md` for the complete
freeze declaration and the narrowly-scoped exceptions under which future
changes are permitted.
