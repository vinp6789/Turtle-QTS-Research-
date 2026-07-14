# 02. Strategy

## Trading Philosophy

Turtle QTS is a regime-gated, conviction-scored, volatility-scaled swing
trading strategy for crypto assets. Entries require both a favorable BTC
market regime and a sufficiently high multi-factor conviction score on
the individual asset. Exits are governed by a fixed stop and two
profit-taking targets, scaled to the asset's own recent volatility, plus
a discretionary exit on regime flip or conviction decay. Position sizing
and portfolio-level risk controls are handled separately from entry/exit
logic (see `04_Portfolio_Engine.md`).

## Entry Logic

An entry is taken when, for a given asset on a given day:

- The BTC regime is not RISK_OFF (see Regime Filters below), and
- The asset's conviction score is greater than or equal to
  `CONV_THRESHOLD` (default 70).

When more entry-eligible candidates exist on a day than available
position slots (`max_positions`, if set), the highest-conviction
candidates are admitted first; lower-conviction candidates are skipped
for that day but remain eligible on a later day if the signal persists.

Optional additional entry filters, each independently toggleable via CLI
flag and each off by default: `--rsi-max` (skip entries when RSI(14)
exceeds the given value) and `--token-trend-filter` (require the asset's
own price above its own 200-day EMA, in addition to the BTC regime
check).

## Exit Logic

For an open position, exits are evaluated in this fixed precedence order
each day:

1. **Stop.** If the day's Low (or close, when High/Low data is
   unavailable) reaches or breaches the stop price, the position closes
   at the stop price. Stop always takes precedence over a target if both
   are breached on the same candle (see C2 in `06_C1_C6_Corrections.md`).
2. **T1 (partial).** If not already hit, and the day's High (or close)
   reaches T1 (entry + 1.5x stop distance), half the position closes at
   T1; the stop is trailed to breakeven (entry price) for the remaining
   half.
3. **T2.** If the day's High (or close) reaches T2 (entry + 3x stop
   distance), the remaining position closes at T2.
4. **Signal loss.** If the BTC regime flips to RISK_OFF, or the asset's
   conviction score falls more than 10 points below `CONV_THRESHOLD`, the
   remaining position closes at the current close price.

If none of the above trigger and the maximum holding period is reached
(used only in the label-generation path, `simulate_trade_outcome`, not in
the live engines), the position closes at the last available price.

Exit price detection uses intrabar High/Low when available (C2); the fill
price itself remains the exact barrier level (stop/T1/T2), not a modeled
gap-through price.

## Position Sizing

Per-trade sizing (fraction of the entry capital risked) is fixed by
construction: the stop distance defines 1R, and profit targets are
defined as fixed multiples of that distance (T1 = 1.5R, T2 = 3R).
Portfolio-level sizing (how much capital, as a percentage of current
equity, a given trade's R-multiple is applied against) is a separate
concern handled by the portfolio engine -- see `04_Portfolio_Engine.md`
for `compute_position_weight`, sizing modes, risk profiles, and
correlation-aware weight adjustment.

## Conviction Scoring

Two scoring paths exist:

**Legacy scorer** (`conviction`/`conviction_dyn`, used by `run` and
`run_dynamic`): a hand-set additive scorecard out of 100, base score 30,
plus:

| Factor | Points | Condition |
|---|---|---|
| Relative strength | +25 | 30-day return exceeds BTC's 30-day return |
| Trend | +20 | Close above 50-day EMA |
| Volume expansion | +15 | 7-day average volume exceeds 23-day-prior average volume by more than 15% |
| Absolute momentum | +10 | 30-day return exceeds 10% |

Score is capped at 100.

**Generalized feature-registry scorer** (`conviction_components`/
`conviction_components_v2`, used by `run_dynamic_attr` and all
attribution/audit commands): the same four factors, reimplemented as
pluggable `ConvictionFeature` subclasses with weights 25/20/15/10
respectively (`RelativeStrengthFeature`, `TrendFeature`,
`VolumeExpansionFeature`, `MomentumFeature`), verified to reproduce the
legacy scorer's binary in/out decisions exactly. Additional features
(`ATRDonchianSqueezeFeature`, `OBVFeature`,
`CrossSectionalReturnRankFeature`, `SharedSeriesFeature`,
`PerAssetExternalSeriesFeature`) can be added to the registry without
modifying the four legacy features, gated behind explicit CLI flags
(`--use-atr`, `--use-obv`, `--use-xsrank`, `--use-shared-series`,
`--use-per-asset-series`).

## Feature Descriptions

- **Relative Strength** -- 30-day return relative to BTC's 30-day
  return. Binary trigger: token outperforms BTC over the trailing 30
  days.
- **Trend** -- close price relative to its own 50-day EMA. Binary
  trigger: close above the 50-day EMA.
- **Volume Expansion** -- 7-day average volume relative to the prior
  23-day average volume. Binary trigger: more than 15% expansion.
- **Momentum** -- 30-day absolute return. Binary trigger: more than 10%.
- **ATR/Donchian Squeeze** -- volatility contraction measure (additive,
  optional, requires High/Low data).
- **OBV** -- On-Balance Volume (additive, optional, uses existing
  Close/Volume data only).
- **Cross-Sectional Return Rank** -- 30-day return percentile-ranked
  against the full universe (additive, optional, requires universe-wide
  visibility).
- **Shared Series / Per-Asset External Series** -- generic hooks for a
  single universe-wide external data series or a per-asset external data
  series (e.g., funding rate, open interest), loaded from CSV (additive,
  optional).

## Regime Filters

The sole regime filter is a BTC-level check: RISK_OFF is defined as BTC's
close falling more than 0.5% below its own 200-day EMA (a deadband to
avoid flip-flopping exactly at the EMA level). No token-level regime
filter exists beyond the optional `--token-trend-filter` entry gate
described above.

## Assumptions

- Daily OHLCV data is representative of tradeable prices (no explicit
  bid/ask spread beyond the fixed slippage assumption).
- Fees and slippage are modeled as fixed percentages (`FEE = 0.001`,
  `SLIP = 0.001`), applied per the corrected R-unit conversion (C1), not
  as a function of order size or venue.
- A single position per symbol at a time; no pyramiding or scaling into
  an existing position.
- Position sizing at the strategy level is fixed-fractional-of-1R;
  portfolio-level capital allocation is a separate, later-stage concern.

## Limitations

- The strategy's own diagnostics (established prior to the C1-C6
  correction process) found no statistically demonstrated OHLCV-derived
  edge (meta-label out-of-sample AUC approximately 0.4837, below 0.50).
  See `05_Research_Findings.md`.
- No token-level regime filter beyond the optional trend filter; a
  token can be signaled purely on relative strength/momentum/volume
  even during a token-specific downtrend, provided the BTC regime is not
  RISK_OFF.
- Universe composition is fixed by whichever CSV files or watchlist are
  supplied; see `08_Known_Limitations.md` for survivorship
  considerations.
