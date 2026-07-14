# 11. CLI Reference

This document covers every command-line flag implemented in
`turtle_backtest.py`. Flags are grouped into: data-input flags,
watchlist-control flags, mode-selecting commands (each produces a
distinct report and represents a distinct "command"), feature-framework
toggle flags, and shared modifier flags consumed by more than one
command. Run `python turtle_backtest.py --help` for the tool's own
generated flag list.

All commands require exactly one data source: `--synthetic`,
`--api-key`, or `--csv-dir`. Most commands other than the default report
and `--sweep`/`--validate` in API/synthetic mode require `--csv-dir`
specifically, as noted per command below.

---

## Data-Input Flags

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `--api-key KEY` | string | `""` | CoinGecko API key. Fetches historical data over the network. |
| `--days N` | int | `365` | Days of history to fetch in API mode (capped at 365/call by CoinGecko's free tier; chunked internally). |
| `--synthetic` | flag | off | Deterministic engine self-test on synthetic random-walk data. No network, no files required. |
| `--csv-dir PATH` | string | `""` | Folder containing CryptoDataDownload-format daily OHLCV CSVs (e.g. `BTCUSDT_day.csv`). Required by most commands below. |

**Category:** Research (data ingestion, used by every command).

---

## Watchlist Control Flags

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `--watchlist "id:SYM,..."` | string | `""` | Replaces the default watchlist entirely, e.g. `"bitcoin:BTC,ethereum:ETH"`. |
| `--add "id:SYM,..."` | string | `""` | Adds entries to the current watchlist without removing existing ones. |
| `--remove "SYM,..."` | string | `""` | Removes symbols from the current watchlist by symbol name. |
| `--conv-threshold N` | int | `None` (uses built-in `70`) | Overrides the conviction entry threshold globally for the run. |

**Category:** Research (universe configuration, applies to every command).

**Notes:** the default watchlist (`DEFAULT_TOKENS`) is BTC, ETH, SOL,
HNT, AAVE, LDO, LINK, ADA, LTC, BNB. In `--csv-dir` mode, only symbols
with a matching CSV file are actually loaded; unmatched watchlist entries
are skipped with a printed notice.

---

## Default Report (no mode flag)

**Purpose:** runs the core entry/exit engine over the full available
history and prints a single trade-level report (trade count, win rate,
avg R, expectancy, best/worst, per-reason and per-asset breakdown,
conviction-tier comparison).

**Syntax:**
```
python turtle_backtest.py --synthetic
python turtle_backtest.py --csv-dir DIR
python turtle_backtest.py --api-key KEY
```

**Parameters:** `--max-positions`, `--rsi-max`, `--token-trend-filter`,
`--dump-trades` (all optional; see Shared Modifier Flags).

**Default values:** no position cap, no RSI filter, no trend filter.

**Example usage:**
```
python turtle_backtest.py --csv-dir ./data --max-positions 3 --dump-trades
```

**Output produced:** trade-level summary to stdout; full chronological
trade log if `--dump-trades` is set.

**Related commands:** `--validate` (multi-fold version of the same
configuration), `--portfolio-report` (portfolio-level metrics instead of
trade-level).

**Notes:** in `--csv-dir` mode, this uses `run_dynamic()` (dynamic
universe); in `--synthetic`/`--api-key` mode, it uses `run()` (legacy
shared-index engine).

**Category:** Research.

---

## `--sweep`

**Purpose:** grid-searches `conv-threshold x max-positions x rsi-max x
token-trend-filter`, validated with a chronological in-sample/
out-of-sample split to guard against overfitting to one window.

**Syntax:** `python turtle_backtest.py --csv-dir DIR --sweep`

**Parameters:** `--sweep-split` (float, default `0.7`; fraction of
history used as in-sample).

**Example usage:** `python turtle_backtest.py --csv-dir ./data --sweep --sweep-split 0.6`

**Output produced:** grid-search results table with in-sample and
out-of-sample statistics per configuration.

**Related commands:** `--validate` (evaluate one candidate configuration
across multiple folds, ideally after `--sweep` suggests one).

**Notes:** in CSV mode, prints a notice that it still uses the legacy
shared-index engine and truncates all assets to a common length,
recommending `--validate` for the dynamic-universe multi-fold path.

**Category:** Research.

---

## `--validate`

**Purpose:** validates the current fixed configuration across multiple
expanding-window chronological folds, plus BTC bull/bear regime
stratification. Does not search parameters.

**Syntax:** `python turtle_backtest.py --csv-dir DIR --validate`

**Parameters:** `--n-folds` (int, default `4`), `--max-positions`,
`--rsi-max`, `--token-trend-filter`.

**Example usage:** `python turtle_backtest.py --csv-dir ./data --validate --n-folds 5`

**Output produced:** per-fold statistics plus regime-stratified results.

**Related commands:** `--sweep` (parameter search), `--purged-validation`
(adds purging/embargo and leakage verification on top of the same
fold structure).

**Category:** Validation.

---

## `--attribute`

**Purpose:** feature attribution -- breaks the conviction score into its
components (`relative_strength`, `trend`, `volume_expansion`,
`momentum`, plus any enabled additive features) and measures each
component's contribution via ablation, correlation plus permutation
test, information gain, and redundancy analysis.

**Syntax:** `python turtle_backtest.py --csv-dir DIR --attribute`

**Parameters:** `--n-folds`, `--max-positions`, `--rs-mode`,
`--rs-window`, `--rs-slope-lookback`, `--use-atr`, `--use-obv`,
`--weights`.

**Example usage:** `python turtle_backtest.py --csv-dir ./data --attribute --use-atr`

**Output produced:** per-component ablation results, correlation and
permutation-test statistics, information gain, redundancy matrix.

**Related commands:** `--diagnose-component` (single-component deep
dive), `--rs-lookback-sweep` (relative-strength window sensitivity).

**Notes:** requires `--csv-dir`.

**Category:** Research.

---

## `--rs-lookback-sweep`

**Purpose:** tests `relative_strength` with lookback windows 7/14/21/30
days, holding everything else constant, to check whether the 30-day
window is causing lag.

**Syntax:** `python turtle_backtest.py --csv-dir DIR --rs-lookback-sweep`

**Parameters:** `--n-folds`, `--max-positions`.

**Example usage:** `python turtle_backtest.py --csv-dir ./data --rs-lookback-sweep`

**Output produced:** comparison table across lookback windows.

**Related commands:** `--attribute`.

**Category:** Research.

---

## `--diagnose-component NAME`

**Purpose:** direct "enabled vs. blocked" trade-set comparison for one
named component (e.g. `relative_strength`), testing whether it filters
out higher-expectancy trades than the ones it lets in.

**Syntax:** `python turtle_backtest.py --csv-dir DIR --diagnose-component relative_strength`

**Parameters:** `--n-folds`, `--max-positions`, `--rs-mode`,
`--rs-window`, `--rs-slope-lookback`, `--use-atr`, `--use-obv`,
`--weights`.

**Example usage:** `python turtle_backtest.py --csv-dir ./data --diagnose-component trend`

**Output produced:** enabled-vs-blocked trade set comparison.

**Related commands:** `--attribute`.

**Category:** Research.

---

## `--train-model`

**Purpose:** learns feature weights via L2-regularized logistic
regression, one model per expanding-window fold (training data only,
never touching that fold's held-out period). Reports per-fold
coefficients, cross-fold stability, bootstrap confidence intervals, and
a comparison against the current hand-set scorecard weights.

**Syntax:** `python turtle_backtest.py --csv-dir DIR --train-model`

**Parameters:** `--n-folds`, `--l2` (float, default `1.0`),
`--n-bootstrap` (int, default `200`), `--max-holding` (int, default
`90`), `--use-atr`, `--use-obv`.

**Example usage:** `python turtle_backtest.py --csv-dir ./data --train-model --l2 0.5`

**Output produced:** per-fold logistic regression coefficients, bootstrap
confidence intervals, comparison to hand-set weights.

**Related commands:** `--purged-validation` (fits the same model on
purged data), `--audit-probability` (evaluates the trained model's
out-of-sample calibration).

**Category:** Research.

---

## `--audit-data`

**Purpose:** Phase 1 data integrity audit. Checks the raw CSVs for gaps,
duplicates, missing/unparseable fields, zero-volume days, flat-price
days, OHLC consistency violations, statistical outliers, and a
wash-trading screening heuristic. Read-only -- does not run any
simulation.

**Syntax:** `python turtle_backtest.py --csv-dir DIR --audit-data`

**Parameters:** none beyond `--csv-dir` (required; exits with an error if
absent).

**Example usage:** `python turtle_backtest.py --csv-dir ./data --audit-data`

**Output produced:** per-file integrity report.

**Related commands:** `--audit-warmup`, `--audit-labels`.

**Category:** Diagnostics.

---

## `--audit-warmup`

**Purpose:** Phase 1b feature coverage and warm-up audit. Per-asset/
per-feature first-valid dates, cross-asset comparison, effective sample
sizes (full-history and per-fold), empirical causality/leakage
verification, and feature synchronization analysis.

**Syntax:** `python turtle_backtest.py --csv-dir DIR --audit-warmup`

**Parameters:** `--n-folds`, `--use-atr`, `--use-obv`.

**Example usage:** `python turtle_backtest.py --csv-dir ./data --audit-warmup --use-atr`

**Output produced:** warm-up and causality audit report.

**Related commands:** `--audit-data`, `--audit-labels`.

**Category:** Diagnostics.

---

## `--audit-labels`

**Purpose:** Phase 2 label audit. Documents exact label-generation logic,
verifies diagnostic tooling matches the real, unmodified
`build_training_samples()` exactly, measures label overlap/uniqueness
(Lopez de Prado average uniqueness), class balance, holding-period
distribution, exit-reason distribution, and fold-boundary label leakage.

**Syntax:** `python turtle_backtest.py --csv-dir DIR --audit-labels`

**Parameters:** `--n-folds`, `--max-holding`, `--use-atr`, `--use-obv`.

**Example usage:** `python turtle_backtest.py --csv-dir ./data --audit-labels`

**Output produced:** label-fidelity and leakage audit report.

**Related commands:** `--purged-validation`.

**Category:** Diagnostics / Validation.

---

## `--purged-validation`

**Purpose:** institution-grade Purged Walk-Forward CV (Lopez de Prado).
Purges training samples whose label window overlaps the test fold,
applies an embargo (equal to `max_holding`) after every prior fold,
verifies zero label leakage, fits logistic regression on purged vs.
unpurged data, and compares downstream test-period trading performance
and feature rankings before and after purging. Does not affect
`--validate` or `--train-model`.

**Syntax:** `python turtle_backtest.py --csv-dir DIR --purged-validation`

**Parameters:** `--n-folds`, `--use-atr`, `--use-obv`, `--l2`,
`--n-bootstrap`, `--max-holding`, `--max-positions`.

**Example usage:** `python turtle_backtest.py --csv-dir ./data --purged-validation`

**Output produced:** purged-vs-unpurged comparison report.

**Related commands:** `--train-model`, `--audit-labels`.

**Category:** Validation.

---

## `--audit-probability`

**Purpose:** read-only audit of the native logistic model's out-of-sample
probability output (purged training, no evaluation/trading path):
distribution statistics, histogram, winner/loser separation, ROC AUC,
PR AUC, Brier score, 10-bin calibration curve.

**Syntax:** `python turtle_backtest.py --csv-dir DIR --audit-probability`

**Parameters:** `--n-folds`, `--use-atr`, `--use-obv`, `--l2`,
`--max-holding`, `--candidate-only`, `--representation`,
`--exclude-feature`.

**Example usage:** `python turtle_backtest.py --csv-dir ./data --audit-probability --candidate-only --representation decile`

**Output produced:** probability calibration and separation report.

**Related commands:** `--train-model`, `--purged-validation`.

**Notes:** `--candidate-only` restricts to the candidate population
(hand-set score >= 70, regime-on) instead of every asset-day, training
and scoring only on candidates; runs native/quartile/decile
representations for ATR+OBV and reports a comparison table.
`--exclude-feature NAME` runs a feature-ablation experiment inside the
candidate-only pipeline (requires `--candidate-only`; uses
representation=native regardless of `--representation`).

**Category:** Diagnostics.

---

## `--audit-forward-return`

**Purpose:** final diagnostic -- tests whether the active features carry
linear/monotonic information about continuous 30-day forward return,
independent of the triple-barrier label entirely. Per-asset and pooled
Pearson/Spearman/joint-R^2 with permutation significance and bootstrap
confidence intervals. No regime stratification, no BTC-normalization, no
discretization.

**Syntax:** `python turtle_backtest.py --csv-dir DIR --audit-forward-return`

**Parameters:** `--n-folds`, `--use-atr`, `--use-obv`, `--max-holding`.

**Example usage:** `python turtle_backtest.py --csv-dir ./data --audit-forward-return`

**Output produced:** forward-return correlation and significance report.
See `05_Research_Findings.md` for the established conclusion this audit
produced.

**Related commands:** `--nonlinear-benchmark`.

**Notes:** diagnostic only; does not affect trading logic.

**Category:** Research.

---

## `--nonlinear-benchmark`

**Purpose:** the one additive experiment testing whether a nonlinear
model (XGBoost, then LightGBM, then RandomForest, first available)
extracts materially more signal from the frozen feature set than the
linear baseline. Reuses population, folds, purge, embargo, and labels
unmodified.

**Syntax:** `python turtle_backtest.py --csv-dir DIR --nonlinear-benchmark`

**Parameters:** `--n-folds`, `--use-atr`, `--use-obv`, `--max-holding`.

**Example usage:** `python turtle_backtest.py --csv-dir ./data --nonlinear-benchmark`

**Output produced:** linear-vs-nonlinear R^2 comparison and materiality
verdict.

**Related commands:** `--audit-forward-return`.

**Notes:** requires `xgboost`, `lightgbm`, or `scikit-learn` to be
installed (`pip install xgboost shap --break-system-packages`). This is
the only command in the tool with an external dependency; every other
command is dependency-free.

**Category:** Research.

---

## `--portfolio-report`

**Purpose:** production-phase deliverable. Computes CAGR, Sharpe,
Sortino, Calmar, Max Drawdown %, Profit Factor, and Recovery Factor from
a real compounded equity curve built from pooled out-of-sample trades.

**Syntax:** `python turtle_backtest.py --csv-dir DIR --portfolio-report`

**Parameters:** `--n-folds`, `--max-positions`, `--use-atr`, `--use-obv`,
`--initial-capital` (float, default `10000.0`), `--risk-pct-per-trade`
(float, default `0.015`), `--sizing-mode` (choices `fixed`,
`vol_targeted`, `conviction_weighted`; default `fixed`).

**Example usage:** `python turtle_backtest.py --csv-dir ./data --portfolio-report --sizing-mode vol_targeted`

**Output produced:** full portfolio performance report (see
`04_Portfolio_Engine.md`).

**Related commands:** `--risk-profiles`, `--optimize-portfolio`.

**Category:** Portfolio Analysis.

---

## `--optimize-portfolio`

**Purpose:** runs the full autonomous portfolio-optimization queue:
Monte Carlo robustness, correlation analysis, sizing-mode comparison,
position-cap sensitivity, and transaction-cost sensitivity, each compared
against the frozen baseline with an explicit keep/reject decision.

**Syntax:** `python turtle_backtest.py --csv-dir DIR --optimize-portfolio`

**Parameters:** `--n-folds`, `--use-atr`, `--use-obv`, `--max-positions`
(default `3` if unset), `--initial-capital`, `--risk-pct-per-trade`.

**Example usage:** `python turtle_backtest.py --csv-dir ./data --optimize-portfolio`

**Output produced:** sequenced experiment report with keep/reject
decisions.

**Related commands:** `--portfolio-report`, `--risk-profiles`.

**Category:** Portfolio Analysis.

---

## `--risk-profiles`

**Purpose:** runs GROWTH, BALANCED, and CAPITAL_PRESERVATION risk
profiles against the same frozen entry logic, reporting CAGR, Sharpe,
Sortino, Calmar, Max Drawdown, Profit Factor, Recovery Factor, and Risk
of Ruin for each, plus an empirical check on whether conviction-weighted
sizing is justified for GROWTH.

**Syntax:** `python turtle_backtest.py --csv-dir DIR --risk-profiles`

**Parameters:** `--n-folds`, `--use-atr`, `--use-obv`,
`--initial-capital`.

**Example usage:** `python turtle_backtest.py --csv-dir ./data --risk-profiles`

**Output produced:** per-profile comparison table (see
`04_Portfolio_Engine.md` for the three profiles' fixed parameters).

**Related commands:** `--portfolio-report`, `--optimize-portfolio`.

**Category:** Portfolio Analysis.

---

## Feature Framework Toggle Flags

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `--use-atr` | flag | off | Enables the generalized feature framework and adds ATR/Donchian squeeze as a 5th conviction feature (requires CSVs with High/Low columns). |
| `--use-obv` | flag | off | Enables the generalized feature framework and adds On-Balance Volume as a conviction feature (Close/Volume only). Combine with `--use-atr` to test both together. |
| `--use-xsrank` | flag | off | Adds Cross-Sectional Return Rank (30-day return percentile-ranked against the full universe) as a 7th feature, alongside whichever base registry is active. |
| `--use-shared-series` | flag | off | Adds a shared, universe-wide series feature (default: volume-based BTC dominance, computed from already-loaded OHLCV). |
| `--shared-series-csv PATH` | string | `""` | Two-column (date,value) CSV to use instead of the default volume-dominance proxy. Requires `--use-shared-series`. |
| `--use-per-asset-series` | flag | off | Adds a per-asset external series feature (e.g. funding rate, open interest). Looks for one CSV per asset matching `--per-asset-pattern`. |
| `--per-asset-pattern PATTERN` | string | `"{sym}_funding.csv"` | Filename pattern (with `{sym}` placeholder) for per-asset series CSVs. Requires `--use-per-asset-series`. |
| `--weights "name:val,..."` | string | `""` | Comma-separated learned-weight overrides for named features, e.g. `"trend:0.412,momentum:-0.087"`. Applies to `--attribute` and `--diagnose-component`; requires `--use-atr`/`--use-obv` so the named features exist in the active registry. |

**Category:** Research (feature-framework configuration, applies across
multiple commands as noted per flag above).

**Notes:** none of these flags modify or replace the four legacy
features (`relative_strength`, `trend`, `volume_expansion`,
`momentum`); they are strictly additive to the active registry.

---

## Shared Modifier Flags

Flags consumed by more than one command above.

| Flag | Type | Default | Applies to |
|---|---|---|---|
| `--max-positions N` | int | `None` (unlimited) | Default report, `--validate`, `--attribute`, `--diagnose-component`, `--rs-lookback-sweep`, `--purged-validation`, `--portfolio-report`, `--optimize-portfolio`. |
| `--rsi-max VALUE` | float | `None` (off) | Default report, `--validate`. |
| `--token-trend-filter` | flag | off | Default report, `--validate`. |
| `--dump-trades` | flag | off | Default report. Prints the full chronological trade log. |
| `--n-folds N` | int | `4` | Every fold-based validation/audit/attribution/portfolio command. |
| `--rs-mode {level,slope}` | string | `"level"` | `--attribute`, `--diagnose-component`. |
| `--rs-window N` | int | `10` | `--attribute`, `--diagnose-component` when `--rs-mode slope`. |
| `--rs-slope-lookback N` | int | `10` | `--attribute`, `--diagnose-component` when `--rs-mode slope`. |
| `--l2 VALUE` | float | `1.0` | `--train-model`, `--purged-validation`, `--audit-probability`. |
| `--n-bootstrap N` | int | `200` (`100` default inside `--purged-validation`) | `--train-model`, `--purged-validation`. |
| `--max-holding N` | int | `90` | `--train-model`, `--audit-labels`, `--purged-validation`, `--audit-probability`, `--audit-forward-return`, `--nonlinear-benchmark`. |
| `--candidate-only` | flag | off | `--audit-probability`. |
| `--representation {native,quartile,decile,all}` | string | `"all"` | `--audit-probability` when `--candidate-only`. |
| `--exclude-feature NAME` | string | `""` | `--audit-probability` when `--candidate-only`. |
| `--initial-capital VALUE` | float | `10000.0` | `--portfolio-report`, `--optimize-portfolio`, `--risk-profiles`. |
| `--risk-pct-per-trade VALUE` | float | `0.015` | `--portfolio-report`, `--optimize-portfolio`. |
| `--sizing-mode {fixed,vol_targeted,conviction_weighted}` | string | `"fixed"` | `--portfolio-report`. |
| `--sweep-split VALUE` | float | `0.7` | `--sweep`. |

---

## Command Category Summary

| Category | Commands |
|---|---|
| Research | Default report, `--sweep`, `--attribute`, `--rs-lookback-sweep`, `--diagnose-component`, `--train-model`, `--audit-forward-return`, `--nonlinear-benchmark`, feature-framework toggle flags |
| Validation | `--validate`, `--purged-validation`, `--audit-labels` |
| Diagnostics | `--audit-data`, `--audit-warmup`, `--audit-probability` |
| Portfolio Analysis | `--portfolio-report`, `--optimize-portfolio`, `--risk-profiles` |
