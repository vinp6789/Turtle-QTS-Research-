# 05. Research Findings

This document summarizes only the final, established research
conclusions for the Turtle QTS OHLCV-derived feature family. It does not
list intermediate hypotheses, rejected experimental branches, or
in-progress work; those are out of scope per project decision. Where a
conclusion was reached in prior research sessions and is reflected in the
source code's conditional reporting logic (rather than being a fixed,
hardcoded string), that is noted explicitly.

## OHLCV Research

Documented project decision (established finding from the completed
research phase): exhaustive testing of the six-feature OHLCV-derived
family (relative strength, trend, volume expansion, momentum, ATR/
Donchian squeeze, OBV) found no statistically meaningful predictive
information for forward returns. Pooled cross-feature correlation with
forward return was in the range of approximately |r| = 0.05-0.08, and
joint R^2 was approximately 1.3% -- both below the practical-significance
thresholds the project's own audit code applies (`|r| > 0.05` as the
"small but real" bar; `R^2 > 0.03` combined with more than double the
linear baseline as the bar for a "material" nonlinear improvement; see
`run_forward_return_audit` and `run_nonlinear_benchmark` in the source).

## Indicator Research

The four legacy indicators (relative strength, trend, volume expansion,
momentum) and two additive indicators (ATR/Donchian squeeze, OBV) were
tested individually and in combination via `run_attribution`
(ablation, correlation plus permutation test, information gain, pairwise
redundancy). Documented project decision: no individual indicator or
combination from this family was found to carry practically meaningful,
statistically significant forward-return information once leakage,
population mismatch, and representation issues were ruled out.

## Feature Importance

The source's attribution tooling (`run_attribution`,
`information_gain`, `phi_coefficient`) exists specifically to rank each
feature's independent contribution and detect redundancy between
features. Documented project decision: none of the six OHLCV-derived
features demonstrated a defensible, independent, statistically
significant contribution to forward-return prediction once redundancy
and leakage were controlled for.

## Nonlinear Experiments

**Function:** `run_nonlinear_benchmark`. Tests whether a nonlinear model
(XGBoost, then LightGBM, then RandomForest, first available) extracts
materially more signal from the same frozen six-feature set than the
linear baseline, using identical population, folds, purge, embargo, and
labels. The source's own materiality bar: R^2 must exceed 0.03 AND more
than double the linear baseline's R^2.

Documented project decision (established finding): the nonlinear model
did not clear this bar. Conclusion: the information ceiling lies in the
feature set itself, not in the linear model's limited expressiveness.
This closes the question of whether a more sophisticated model, rather
than new information, was the missing ingredient.

## Orthogonal Data Research

The source's own conclusion text (in `run_forward_return_audit` and
`run_nonlinear_benchmark`) explicitly redirects further research toward
information sources orthogonal to OHLCV: market breadth, derivatives
(funding rates, open interest), cross-asset signals, on-chain metrics,
macro liquidity, options positioning, and stablecoin flows -- rather than
adding further price-derived indicators (RSI, MACD, ADX, or similar) from
the same already-tested information family.

Infrastructure for consuming such external series exists in the source
(`SharedSeriesFeature`, `PerAssetExternalSeriesFeature`,
`load_shared_series_from_csv`, `load_per_asset_series_from_csvs`,
`--use-shared-series`, `--use-per-asset-series`), but as of the frozen
source, only a volume-derived BTC-dominance proxy (computed from already-
loaded OHLCV, used as a proof of the architecture) has been exercised
through this infrastructure. No funding, open interest, or on-chain data
has been loaded or tested in this source.

## Open Interest Conclusions

Documented project decision: Open Interest was identified in prior
research sessions as the highest-priority untested orthogonal data
source, blocked only by data availability at the time. No Open Interest
data has been loaded, tested, or evaluated within this source. There is
no finding to report; this remains future work.

## Funding Conclusions

Documented project decision: funding-rate data has not been loaded,
tested, or evaluated within this source. The per-asset external series
infrastructure supports a funding-rate CSV by filename pattern
(`--per-asset-pattern`, default `{sym}_funding.csv`), but no funding data
has been supplied or run through it. There is no finding to report; this
remains future work.

## Stocks vs. Crypto Lessons

Documented project decision: no equity/stock research has been conducted
within this source. The engine, feature framework, and validation
methodology are crypto-specific in their data assumptions (24/7 markets,
no session gaps in the intrabar High/Low handling, CoinGecko/CryptoData
Download-format ingestion). Any stock-market application is explicitly
out of scope for this engine (see `07_Architecture_Freeze.md`).

## What Was Permanently Rejected

- Further OHLCV-derived technical indicator engineering (RSI, MACD, ADX,
  or similar) on top of the six already-tested features, given negligible
  and non-significant explanatory power across every representation
  tested (binary, continuous, cross-sectional rank, shared-series).
- The hypothesis that a more expressive (nonlinear) model was the
  missing ingredient, given the nonlinear benchmark's failure to clear
  its own materiality bar against the same frozen feature set.

## What Remains Future Work

- Loading and evaluating Open Interest data (highest priority, per prior
  research sessions).
- Loading and evaluating funding-rate data.
- Loading and evaluating on-chain metrics, macro liquidity, options
  positioning, and stablecoin flow data.
- Any evaluation of these orthogonal sources would use the existing,
  frozen `SharedSeriesFeature`/`PerAssetExternalSeriesFeature`
  infrastructure and the existing, frozen validation methodology
  (`03_Validation.md`); per the architecture freeze, this would be
  additive data-source evaluation, not a change to strategy, indicators,
  or architecture.
