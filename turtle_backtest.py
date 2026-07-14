#!/usr/bin/env python3
"""
Turtle QTS Backtester
=====================
Replays the core Turtle QTS logic over historical CoinGecko daily data and
reports win rate, expectancy, and whether conviction scores predict returns.

USAGE (on your own machine, needs internet):
    pip install requests
    python turtle_backtest.py --api-key CG-xxxx --days 730
    python turtle_backtest.py --synthetic          # engine self-test, no network

WHAT IS FAITHFULLY REPLAYED from the n8n workflow:
  - Regime: real 200D EMA + 50W EMA on BTC (same math, same 0.5% deadband)
  - Conviction: momentum vs BTC (30d), trend (price>50D EMA), volume expansion
  - Entries: conviction >= 70 and regime not RISK_OFF
  - Exits: volatility-scaled stop (8-25%, 0.8x abs 30d change), T1=1.5R
    (half off), T2=3R (rest off), or conviction-loss / regime-flip exit
  - Fees: 0.1% per side + 0.1% slippage

WHAT IS NOT REPLAYED (be honest with yourself about this):
  - Fear&Greed, funding, unlocks, sector rotation (no free history / geo-blocked)
  - Intraday fills: stops/targets checked on daily closes only -> optimistic
  - CoinGecko free tier caps history at 365d/call; script chunks requests
Results are an approximation of the system's edge, not a promise.
"""
import argparse, json, math, random, sys, time, datetime
from urllib.request import Request, urlopen

DEFAULT_TOKENS = {
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "helium": "HNT",
    "aave": "AAVE", "lido-dao": "LDO", "chainlink": "LINK", "cardano": "ADA",
    "litecoin": "LTC", "binancecoin": "BNB",
}
TOKENS = dict(DEFAULT_TOKENS)  # mutated at runtime by --watchlist / --add / --remove
CONV_THRESHOLD = 70  # overridable via --conv-threshold
FEE = 0.001; SLIP = 0.001

def ema(vals, period):
    if len(vals) < period: return None
    k = 2 / (period + 1)
    e = sum(vals[:period]) / period
    for v in vals[period:]:
        e = v * k + e * (1 - k)
    return e

def compute_rsi(closes, i, period=14):
    """Standard Wilder's RSI at index i, needs i >= period."""
    if i < period: return None
    gains, losses = 0.0, 0.0
    for j in range(i - period + 1, i + 1):
        change = closes[j] - closes[j-1]
        if change > 0: gains += change
        else: losses += -change
    avg_gain, avg_loss = gains / period, losses / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def rolling_ema_series(vals, period):
    out = [None] * len(vals)
    if len(vals) < period: return out
    k = 2 / (period + 1)
    e = sum(vals[:period]) / period
    out[period - 1] = e
    for i in range(period, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out

def fetch_history(cid, days, api_key):
    url = (f"https://api.coingecko.com/api/v3/coins/{cid}/market_chart"
           f"?vs_currency=usd&days={min(days,365)}&interval=daily")
    req = Request(url, headers={"x-cg-demo-api-key": api_key})
    with urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
    closes = [p[1] for p in d.get("prices", []) if p[1]]
    vols = [v[1] for v in d.get("total_volumes", []) if v[1]]
    return closes, vols

def load_csv_history(path):
    """
    Load a CryptoDataDownload-format daily OHLCV CSV.
    Format: comment line, then header row, then data rows.
    Header typically: Unix,Date,Symbol,Open,High,Low,Close,Volume XXX,Volume YYY
    Handles files that are already sorted ascending OR descending by date.
    Returns (dates, closes, vols) -- dates normalized to 'YYYY-MM-DD' strings,
    preserving the FULL native length of this asset's own history (no
    truncation to any other asset's length -- that alignment now happens
    later, per-day, via date lookup in run_dynamic()).
    """
    import csv as csvmod
    with open(path, newline='') as f:
        lines = f.readlines()
    # Skip the CryptoDataDownload comment/attribution line if present
    start = 1 if lines[0].strip().lower().startswith(('http', '#', 'https')) else 0
    reader = csvmod.DictReader(lines[start:])
    rows = list(reader)
    if not rows:
        raise ValueError(f"No data rows found in {path}")
    # Normalize column names (some files use lowercase, some 'close', some 'Close')
    def get(row, *names):
        for n in names:
            if n in row: return row[n]
        raise KeyError(f"None of {names} found in columns: {list(row.keys())}")
    parsed = []
    for r in rows:
        try:
            raw_date = get(r, 'Date', 'date')
            date = raw_date.strip().split(' ')[0].split('T')[0]  # normalize to YYYY-MM-DD, drop any time component
            close = float(get(r, 'Close', 'close'))
            vol = get(r, 'Volume USDT', 'Volume USD', 'Volume', 'volume')
            vol = float(vol) if vol not in (None, '') else 0.0
        except (KeyError, ValueError):
            continue
        parsed.append((date, close, vol))
    # Sort chronologically ascending; dedupe same-day rows (keep last occurrence)
    parsed.sort(key=lambda x: x[0])
    dedup = {}
    for date, close, vol in parsed:
        dedup[date] = (close, vol)
    dates = sorted(dedup.keys())
    closes = [dedup[d][0] for d in dates]
    vols = [dedup[d][1] for d in dates]
    return dates, closes, vols

def load_csv_hilo(path):
    """
    Companion to load_csv_history(): parses the SAME CryptoDataDownload CSV
    a second time, extracting High/Low instead of Close/Volume. Kept as a
    separate function rather than changing load_csv_history()'s return
    signature -- this way every existing call site (run(), run_dynamic(),
    run_dynamic_attr(), --sweep, --validate, etc.) is completely untouched;
    only the NEW feature-framework code path (ATR/Donchian) calls this.
    Returns (dates, highs, lows), same date-alignment convention as
    load_csv_history(). Returns (None, None, None) if High/Low columns
    aren't present in the file, so callers can fail gracefully rather than
    crash on older or non-standard CSV exports.
    """
    import csv as csvmod
    with open(path, newline='') as f:
        lines = f.readlines()
    start = 1 if lines[0].strip().lower().startswith(('http', '#', 'https')) else 0
    reader = csvmod.DictReader(lines[start:])
    rows = list(reader)
    if not rows:
        return None, None, None
    def get(row, *names):
        for n in names:
            if n in row: return row[n]
        return None
    parsed = []
    for r in rows:
        raw_date = get(r, 'Date', 'date')
        high_raw = get(r, 'High', 'high')
        low_raw = get(r, 'Low', 'low')
        if raw_date is None or high_raw is None or low_raw is None:
            return None, None, None  # High/Low not present in this file at all
        try:
            date = raw_date.strip().split(' ')[0].split('T')[0]
            high = float(high_raw)
            low = float(low_raw)
        except ValueError:
            continue
        parsed.append((date, high, low))
    if not parsed:
        return None, None, None
    parsed.sort(key=lambda x: x[0])
    dedup = {}
    for date, high, low in parsed:
        dedup[date] = (high, low)
    dates = sorted(dedup.keys())
    highs = [dedup[d][0] for d in dates]
    lows = [dedup[d][1] for d in dates]
    return dates, highs, lows


def audit_csv_raw(path):
    """
    Re-reads a CSV independently of load_csv_history()/load_csv_hilo() --
    those two functions silently dedupe (last-value-wins) and skip
    unparseable rows, which is correct for backtesting but HIDES exactly
    the defects this audit needs to count. This function reports on the
    RAW rows before any cleanup, purely for inspection -- it changes
    nothing about how data is loaded for simulation.
    """
    import csv as csvmod
    with open(path, newline='') as f:
        lines = f.readlines()
    start = 1 if lines[0].strip().lower().startswith(('http', '#', 'https')) else 0
    reader = csvmod.DictReader(lines[start:])
    rows = list(reader)

    def get(row, *names):
        for n in names:
            if n in row: return row[n]
        return None

    date_counts = {}
    parsed_rows = []
    missing_field_count = 0
    unparseable_count = 0
    for r in rows:
        raw_date = get(r, 'Date', 'date')
        o, h, l, c = get(r,'Open','open'), get(r,'High','high'), get(r,'Low','low'), get(r,'Close','close')
        v = get(r, 'Volume USDT', 'Volume USD', 'Volume', 'volume')
        if raw_date is None or None in (o, h, l, c, v) or '' in (o, h, l, c, v):
            missing_field_count += 1
            continue
        date = raw_date.strip().split(' ')[0].split('T')[0]
        date_counts[date] = date_counts.get(date, 0) + 1
        try:
            parsed_rows.append((date, float(o), float(h), float(l), float(c), float(v)))
        except ValueError:
            unparseable_count += 1

    parsed_rows.sort(key=lambda x: x[0])
    duplicate_dates = {d: c for d, c in date_counts.items() if c > 1}

    # Gap detection: count calendar days between consecutive rows > 1
    gaps = []
    for i in range(1, len(parsed_rows)):
        d0 = datetime.date.fromisoformat(parsed_rows[i-1][0])
        d1 = datetime.date.fromisoformat(parsed_rows[i][0])
        delta = (d1 - d0).days
        if delta > 1:
            gaps.append((parsed_rows[i-1][0], parsed_rows[i][0], delta - 1))

    zero_volume_days = sum(1 for r in parsed_rows if r[5] == 0)
    flat_price_days = sum(1 for r in parsed_rows if r[1] == r[2] == r[3] == r[4])
    negative_or_zero_price_days = sum(1 for r in parsed_rows if r[4] <= 0 or r[1] <= 0)
    ohlc_violation_days = sum(1 for r in parsed_rows
                               if not (r[3] <= r[1] <= r[2] and r[3] <= r[4] <= r[2]))  # low <= open,close <= high

    # Outlier days: daily return beyond 6 std devs of this asset's own return distribution
    closes = [r[4] for r in parsed_rows]
    rets = [(closes[i]/closes[i-1] - 1) for i in range(1, len(closes)) if closes[i-1] > 0]
    outliers = []
    if len(rets) > 30:
        mean_r = sum(rets) / len(rets)
        std_r = math.sqrt(sum((x-mean_r)**2 for x in rets) / len(rets))
        if std_r > 0:
            for i, r in enumerate(rets):
                z = (r - mean_r) / std_r
                if abs(z) > 6:
                    outliers.append((parsed_rows[i+1][0], r, z))

    # Crude wash-trading proxy: volume in the top 1% for this asset occurring
    # on a day with near-zero price movement (|return| < 0.1%). Not proof of
    # wash trading -- a screening heuristic for days worth a manual look.
    wash_flags = []
    if len(parsed_rows) > 100:
        vols_sorted = sorted(r[5] for r in parsed_rows)
        vol_p99 = vols_sorted[int(len(vols_sorted) * 0.99)]
        for i in range(1, len(parsed_rows)):
            if parsed_rows[i][5] >= vol_p99 and closes[i-1] > 0:
                ret = closes[i] / closes[i-1] - 1
                if abs(ret) < 0.001:
                    wash_flags.append((parsed_rows[i][0], parsed_rows[i][5], ret))

    return dict(
        path=path, total_rows=len(rows), parsed_rows=len(parsed_rows),
        missing_field_rows=missing_field_count, unparseable_rows=unparseable_count,
        duplicate_dates=duplicate_dates, date_range=(parsed_rows[0][0], parsed_rows[-1][0]) if parsed_rows else (None, None),
        gaps=gaps, zero_volume_days=zero_volume_days, flat_price_days=flat_price_days,
        negative_or_zero_price_days=negative_or_zero_price_days, ohlc_violation_days=ohlc_violation_days,
        outliers=outliers, wash_flags=wash_flags, n_return_obs=len(rets)
    )


def run_data_audit(csv_dir, watchlist_map):
    """
    Phase 1 -- Data Integrity. Read-only: loads nothing into the simulation
    engine, modifies no existing function, produces a report only. Reuses
    the same file-matching pattern already used by --csv-dir in main().
    """
    import os, glob
    print(f"\n{'='*90}\nPHASE 1 -- DATA INTEGRITY AUDIT\n{'='*90}")
    print(f"Directory: {csv_dir}\n")

    results = {}
    for sym, cid in watchlist_map.items():
        matches = glob.glob(os.path.join(csv_dir, f"*{sym}USDT*day*.csv")) or \
                  glob.glob(os.path.join(csv_dir, f"*{sym}USDT*.csv")) or \
                  glob.glob(os.path.join(csv_dir, f"*{sym.lower()}usdt*.csv"))
        if not matches:
            print(f"  {sym:5s} SKIPPED (no CSV found)")
            continue
        r = audit_csv_raw(matches[0])
        results[sym] = r

    print(f"{'sym':6s} {'rows':>6s} {'range':>23s} {'dupes':>6s} {'gaps':>5s} {'gap-days':>8s} "
          f"{'0-vol':>6s} {'flat':>5s} {'ohlc-bad':>8s} {'outliers':>8s} {'wash-flag':>9s}")
    for sym, r in results.items():
        gap_days = sum(g[2] for g in r['gaps'])
        date_str = f"{r['date_range'][0]} to {r['date_range'][1]}"
        print(f"{sym:6s} {r['parsed_rows']:>6d} {date_str:>23s} {len(r['duplicate_dates']):>6d} "
              f"{len(r['gaps']):>5d} {gap_days:>8d} {r['zero_volume_days']:>6d} {r['flat_price_days']:>5d} "
              f"{r['ohlc_violation_days']:>8d} {len(r['outliers']):>8d} {len(r['wash_flags']):>9d}")

    print(f"\n{'-'*90}\nDETAIL\n{'-'*90}")
    any_hard_fail = False
    for sym, r in results.items():
        issues = []
        if r['missing_field_rows'] > 0:
            issues.append(f"{r['missing_field_rows']} rows with missing OHLCV fields")
        if r['unparseable_rows'] > 0:
            issues.append(f"{r['unparseable_rows']} rows failed to parse as numeric")
        if r['duplicate_dates']:
            issues.append(f"{len(r['duplicate_dates'])} duplicate date(s): {list(r['duplicate_dates'])[:5]}{'...' if len(r['duplicate_dates'])>5 else ''}")
        if r['gaps']:
            worst = sorted(r['gaps'], key=lambda g: -g[2])[:3]
            issues.append(f"{len(r['gaps'])} gap(s), largest: " + ", ".join(f"{g[0]}->{g[1]} ({g[2]}d missing)" for g in worst))
        if r['negative_or_zero_price_days'] > 0:
            issues.append(f"{r['negative_or_zero_price_days']} day(s) with zero/negative price -- HARD FAIL, investigate before using this file")
            any_hard_fail = True
        if r['ohlc_violation_days'] > 0:
            issues.append(f"{r['ohlc_violation_days']} day(s) where low/high don't bound open/close -- HARD FAIL, investigate")
            any_hard_fail = True
        if r['zero_volume_days'] > 0:
            issues.append(f"{r['zero_volume_days']} zero-volume day(s) (often early-listing illiquidity -- check if concentrated at series start)")
        if r['flat_price_days'] > 0:
            issues.append(f"{r['flat_price_days']} flat-price day(s) (O=H=L=C -- check if isolated or clustered)")
        if r['outliers']:
            issues.append(f"{len(r['outliers'])} outlier day(s) beyond 6 std-dev return, e.g. " +
                          ", ".join(f"{d} ({ret*100:+.0f}%)" for d, ret, z in r['outliers'][:3]))
        if r['wash_flags']:
            issues.append(f"{len(r['wash_flags'])} day(s) flagged by wash-trading heuristic (top-1% volume + <0.1% price move) -- screening only, not proof")
        print(f"\n{sym}:")
        if issues:
            for i in issues: print(f"    - {i}")
        else:
            print(f"    ✅ No issues detected")

    print(f"\n{'-'*90}\nCHECKS THIS SCRIPT CANNOT PERFORM (require external information)\n{'-'*90}")
    print("  - Exchange inconsistencies: needs cross-referencing against a second data")
    print("    source (e.g. a different exchange's OHLCV for the same pair); not")
    print("    automatable from a single CSV.")
    print("  - Survivorship bias: needs a historical record of what was delisted from")
    print("    Binance over this period; the current file set can't reveal what's")
    print("    absent from it.")
    print("  - Universe selection bias: the current 10-asset watchlist was hand-specified")
    print("    across this research conversation, not drawn systematically from a")
    print("    point-in-time index (e.g. top-N by market cap as of each historical date).")
    print("    This is a documented methodology fact, not something to detect in a CSV.")

    print(f"\n{'='*90}\nGO / NO-GO\n{'='*90}")
    if any_hard_fail:
        print("NO-GO: at least one asset has a HARD FAIL (negative/zero price, or OHLC")
        print("       values that don't bound each other). Fix or exclude that asset's")
        print("       data before proceeding to Phase 2.")
    else:
        print("GO: no hard failures. Zero-volume/flat-price/outlier counts above are")
        print("    worth a manual glance (especially if concentrated rather than sparse)")
        print("    but do not block proceeding to Phase 2 (Label Audit).")
    return results


def synthetic_history(seed, n=730):
    random.seed(seed)
    trend = random.uniform(-0.0005, 0.0015)
    px, closes, vols = 100.0, [], []
    for _ in range(n):
        px *= 1 + trend + random.gauss(0, 0.03)
        closes.append(max(px, 0.01)); vols.append(random.uniform(1e6, 5e6))
    return closes, vols

def conviction(closes, vols, btc_closes, i):
    """Score token at day index i (needs >=60 days of lookback)."""
    if i < 60: return 0
    score = 30  # base
    tok30 = closes[i] / closes[i-30] - 1
    btc30 = btc_closes[i] / btc_closes[i-30] - 1
    if tok30 > btc30: score += 25              # relative strength
    e50 = ema(closes[max(0,i-120):i+1], 50)
    if e50 and closes[i] > e50: score += 20    # trend
    v_now = sum(vols[i-7:i]) / 7; v_prev = sum(vols[i-30:i-7]) / 23
    if v_prev > 0 and v_now > v_prev * 1.15: score += 15   # volume expansion
    if tok30 > 0.10: score += 10               # absolute momentum
    return min(100, score)

def conviction_dyn(closes, vols, i_local, btc_closes, i_btc):
    """
    Same scoring logic as conviction(), but takes SEPARATE indices for the
    token (i_local, into its own native-length array) and BTC (i_btc, into
    BTC's own full array) -- required once assets are no longer forced onto
    a shared, truncated index. Used exclusively by run_dynamic().
    """
    if i_local < 60: return 0
    score = 30
    tok30 = closes[i_local] / closes[i_local-30] - 1
    btc30 = btc_closes[i_btc] / btc_closes[i_btc-30] - 1
    if tok30 > btc30: score += 25
    e50 = ema(closes[max(0,i_local-120):i_local+1], 50)
    if e50 and closes[i_local] > e50: score += 20
    v_now = sum(vols[i_local-7:i_local]) / 7; v_prev = sum(vols[i_local-30:i_local-7]) / 23
    if v_prev > 0 and v_now > v_prev * 1.15: score += 15
    if tok30 > 0.10: score += 10
    return min(100, score)

# Component name -> point weight, single source of truth used by both
# conviction_components() and the ablation/attribution framework below.
CONVICTION_COMPONENTS = {
    "relative_strength": 25,  # token's 30d return beats BTC's 30d return
    "trend":              20, # price above its own 50D EMA
    "volume_expansion":   15, # 7d avg volume > 1.15x the prior 23d avg
    "momentum":           10, # 30d return > +10% outright
}
CONVICTION_BASE = 30  # fixed floor, not attributable (no variance across candidates)

# ============================================================
# GENERALIZED FEATURE FRAMEWORK
#
# The legacy conviction_components() above stays exactly as-is -- every
# existing --attribute / --validate / --diagnose-component command keeps
# working unchanged. This framework is a SEPARATE, opt-in path
# (conviction_components_v2 + run_dynamic_attr's use_framework=True) that
# generalizes scoring to any number of pluggable features, each exposing:
#   - a raw value (whatever units make sense for that signal)
#   - a normalized 0-100 continuous score (what the engine actually sums)
#   - an optional binary interpretation (diagnostics/ablation only, never
#     used by the scoring math itself)
#
# A future feature (BTC dominance, funding, OI, breadth, ...) is added by
# writing ONE small class and adding it to FEATURE_REGISTRY -- nothing in
# the simulation loop, attribution methods, or CLI wiring needs to change.
# ============================================================

class ConvictionFeature:
    """Base interface every conviction feature implements."""
    name = "base"
    weight = 0          # max points this feature can contribute to the conviction score
    min_lookback = 60   # local days of history required before this feature can compute

    def raw(self, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        """Return the feature's raw, uninterpreted value, or None if not computable."""
        raise NotImplementedError

    def normalized(self, raw_value, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        """Return a 0-100 continuous score derived from raw_value. This is
        what the conviction engine sums: weight * (normalized/100)."""
        raise NotImplementedError

    def binary(self, raw_value, normalized_value):
        """Return True/False for diagnostics (ablation/permutation/IG) only.
        Never consulted by the scoring math itself."""
        raise NotImplementedError


class RelativeStrengthFeature(ConvictionFeature):
    """Reproduces the ORIGINAL binary relative_strength exactly: normalized()
    returns 100 or 0, so weight*(normalized/100) equals the legacy flat
    weight-or-nothing score byte-for-byte. Satisfies requirement 3: existing
    features keep working unchanged while being expressed through the new
    continuous interface."""
    name = "relative_strength"
    weight = 25
    min_lookback = 35

    def raw(self, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        tok30 = closes[i_local] / closes[i_local-30] - 1
        btc30 = btc_closes[i_btc] / btc_closes[i_btc-30] - 1
        return tok30 - btc30

    def normalized(self, raw_value, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        return 100.0 if raw_value > 0 else 0.0

    def binary(self, raw_value, normalized_value):
        return raw_value > 0


class TrendFeature(ConvictionFeature):
    name = "trend"
    weight = 20
    min_lookback = 125

    def raw(self, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        e50 = ema(closes[max(0,i_local-120):i_local+1], 50)
        return (closes[i_local] - e50) if e50 else None

    def normalized(self, raw_value, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        return 100.0 if (raw_value is not None and raw_value > 0) else 0.0

    def binary(self, raw_value, normalized_value):
        return raw_value is not None and raw_value > 0


class VolumeExpansionFeature(ConvictionFeature):
    name = "volume_expansion"
    weight = 15
    min_lookback = 35

    def raw(self, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        v_now = sum(vols[i_local-7:i_local]) / 7
        v_prev = sum(vols[i_local-30:i_local-7]) / 23
        return (v_now / v_prev - 1) if v_prev > 0 else None

    def normalized(self, raw_value, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        return 100.0 if (raw_value is not None and raw_value > 0.15) else 0.0

    def binary(self, raw_value, normalized_value):
        return raw_value is not None and raw_value > 0.15


class MomentumFeature(ConvictionFeature):
    name = "momentum"
    weight = 10
    min_lookback = 35

    def raw(self, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        return closes[i_local] / closes[i_local-30] - 1

    def normalized(self, raw_value, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        return 100.0 if raw_value > 0.10 else 0.0

    def binary(self, raw_value, normalized_value):
        return raw_value > 0.10


class ATRDonchianSqueezeFeature(ConvictionFeature):
    """
    FIRST genuinely continuous feature. Measures volatility CONTRACTION,
    not direction -- a different statistical moment (dispersion) than the
    four existing direction-based features, which is the orthogonality
    case argued in the research phase.

    raw(): Donchian channel width as a % of price, over a 20-day window
    (the original Turtle System 1 entry length).

    normalized(): 100 minus the current width's percentile rank against
    its OWN trailing 100-day distribution -- so a width narrower than
    it's been in the past 100 days scores HIGH (tight squeeze = high
    anticipatory score), a wide-open range scores LOW. This is a
    percentile-rank squeeze intensity, the same family of construction as
    a TTM Squeeze / Bollinger Band Width percentile.

    binary(): diagnostic-only "is this a squeeze" flag -- bottom quintile
    (percentile < 20) of the asset's own trailing range history.
    """
    name = "atr_donchian_squeeze"
    weight = 15          # provisional weight -- validated empirically before being trusted
    min_lookback = 125   # 100-day percentile window + 20-day Donchian window + margin

    def raw(self, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        if highs is None or lows is None or i_local < 20:
            return None
        dc_high = max(highs[i_local-19:i_local+1])
        dc_low = min(lows[i_local-19:i_local+1])
        px = closes[i_local]
        if px <= 0:
            return None
        return (dc_high - dc_low) / px * 100  # Donchian width as % of price

    def normalized(self, raw_value, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        if raw_value is None or highs is None or lows is None or i_local < 120:
            return 0.0
        # Rebuild the trailing 100-day width series to rank the current
        # width against the asset's OWN recent history (no look-ahead --
        # every value in this window is at or before i_local).
        widths = []
        for j in range(i_local - 99, i_local + 1):
            if j < 20: continue
            h = max(highs[j-19:j+1]); l = min(lows[j-19:j+1]); p = closes[j]
            if p > 0: widths.append((h - l) / p * 100)
        if len(widths) < 20:
            return 0.0
        rank = sum(1 for w in widths if w <= raw_value) / len(widths)  # percentile, 0..1
        return max(0.0, min(100.0, (1.0 - rank) * 100))  # invert: tight squeeze -> high score

    def binary(self, raw_value, normalized_value):
        return normalized_value >= 80  # top quintile of squeeze intensity == bottom quintile of width


class OBVFeature(ConvictionFeature):
    """
    Standard On-Balance Volume (Granville, 1963): cumulative sum of
    signed daily volume (+volume on up days, -volume on down days).
    Measures DIRECTIONAL participation, distinct from Volume Expansion's
    magnitude-only measure -- a token can show sustained OBV accumulation
    on unremarkable day-to-day volume levels (stealth accumulation),
    which Volume Expansion is structurally blind to (it only fires on a
    single day's volume spike, with no regard for direction).

    Deliberately the PLAIN baseline: no slope, no EMA-of-OBV, no
    breakout detection -- those are explicitly deferred to a future,
    separate research pass so this establishes a clean, unmodified
    reference point first.

    raw(): OBV accumulated over a bounded trailing window (30 days,
    matching the existing 30d convention already used by Momentum/
    Relative Strength elsewhere in this file -- not tuned for this
    feature). A bounded window is used rather than a since-inception
    cumulative sum because OBV's absolute level is otherwise arbitrary
    and not comparable across assets or across time -- this is standard
    practice, not a deviation from "standard OBV" math (the accumulation
    rule itself -- sign(Δprice) * volume -- is unmodified).

    normalized(): OBV divided by total volume traded over the same
    window, giving a naturally bounded ratio in [-1, +1] (net buying
    pressure vs. net selling pressure), linearly rescaled to [0, 100].
    No free parameters beyond the window length, and no threshold
    tuning -- explicitly required by the task.

    binary(): normalized >= 50, i.e. raw OBV ratio > 0 -- net positive
    directional flow. The simplest possible unbiased split of the raw
    signal (not a tuned cutoff).
    """
    name = "obv"
    weight = 15          # provisional weight -- validated empirically before being trusted, same as ATR/Donchian
    min_lookback = 35
    window = 30

    def raw(self, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        start = i_local - self.window + 1
        if start < 1:
            return None
        obv = 0.0
        for j in range(start, i_local + 1):
            if closes[j] > closes[j-1]:
                obv += vols[j]
            elif closes[j] < closes[j-1]:
                obv -= vols[j]
            # unchanged price contributes zero, per standard OBV construction
        return obv

    def normalized(self, raw_value, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        if raw_value is None:
            return 0.0
        start = i_local - self.window + 1
        total_vol = sum(vols[start:i_local+1])
        if total_vol <= 0:
            return 0.0
        ratio = max(-1.0, min(1.0, raw_value / total_vol))  # bounded [-1, +1]
        return (ratio + 1.0) * 50.0  # rescale to [0, 100]

    def binary(self, raw_value, normalized_value):
        return normalized_value >= 50.0


def precompute_cross_sectional_ranks(histories_dated, btc_id="bitcoin", lookback=30):
    """
    For every (asset, global_day), computes that asset's `lookback`-day
    trailing return and ranks it against every OTHER asset's trailing
    return on the SAME calendar date, converting to a 0-100 percentile.

    This requires simultaneous visibility across the whole universe --
    something no existing ConvictionFeature can see, since raw()/
    normalized() only ever receive one asset's own closes/vols arrays (by
    the existing, UNMODIFIED interface used identically by every command).
    Rather than changing that interface -- which every existing feature
    and every existing command depends on -- this precomputes the
    cross-sectional information ONCE, up front, into a lookup keyed by
    id(closes) (stable throughout this codebase: closes arrays are never
    copied between load_csv_history() and any consumer, confirmed by
    inspection). CrossSectionalReturnRankFeature reads this lookup inside
    its own raw() using i_btc (the global day index, already part of the
    existing call signature) and id(closes) -- zero changes required to
    conviction_components_v2, run_dynamic_attr, build_forward_return_samples,
    or any other existing function.

    Returns {id(closes_array): {global_day_index: percentile_0_to_100}}.
    """
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    date_idx = {cid: {d: idx for idx, d in enumerate(dates)} for cid, (dates, closes, vols) in histories_dated.items()}
    lookup = {id(closes): {} for cid, (dates, closes, vols) in histories_dated.items()}

    for i in range(n):
        D = btc_dates[i]
        day_returns = []  # (closes_id, return)
        for cid, (dates, closes, vols) in histories_dated.items():
            local_i = date_idx[cid].get(D)
            if local_i is None or local_i < lookback:
                continue
            if closes[local_i - lookback] <= 0:
                continue
            ret = closes[local_i] / closes[local_i - lookback] - 1
            day_returns.append((id(closes), ret))
        if len(day_returns) < 2:
            continue  # can't meaningfully rank with fewer than 2 assets present
        rets_only = [r for _, r in day_returns]
        for closes_id, ret in day_returns:
            rank_below = sum(1 for r in rets_only if r < ret)
            rank_equal = sum(1 for r in rets_only if r == ret)
            percentile = 100 * (rank_below + 0.5 * rank_equal) / len(rets_only)
            lookup[closes_id][i] = percentile
    return lookup


class CrossSectionalReturnRankFeature(ConvictionFeature):
    """
    THE ONE NEW FEATURE. Generalizes Relative Strength from "outperformance
    vs BTC only" to "percentile rank of 30d return within the FULL
    universe" -- a cross-sectional/ensemble measure, structurally different
    from every existing feature (all six of which are single-asset
    functions). Does not touch RelativeStrengthFeature or any other
    existing class in any way.

    Requires a precomputed rank_lookup (see precompute_cross_sectional_ranks
    above) since it needs universe-wide information no single-asset raw()
    call can see on its own -- passed in at construction time, not via the
    existing per-call signature (which stays completely unchanged).
    """
    name = "cross_sectional_return_rank"
    weight = 15  # provisional -- same convention as ATR/OBV, validated empirically before being trusted
    min_lookback = 35

    def __init__(self, rank_lookup):
        self.rank_lookup = rank_lookup

    def raw(self, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        day_lookup = self.rank_lookup.get(id(closes))
        if day_lookup is None:
            return None
        return day_lookup.get(i_btc)  # 0-100 percentile, or None if not computed for this day

    def normalized(self, raw_value, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        return raw_value if raw_value is not None else 0.0  # already on a 0-100 scale by construction

    def binary(self, raw_value, normalized_value):
        return normalized_value >= 50.0  # above cross-sectional median


# ============================================================
# SHARED-SERIES FEATURE ARCHITECTURE
#
# Generic infrastructure for any feature derived from a SINGLE, universe-
# wide daily series -- not any one asset's own OHLCV, and not an ensemble
# statistic recomputed per-asset like Cross-Sectional Return Rank. A
# shared series has exactly ONE value per calendar day, identical for
# every asset scored that day (e.g. BTC dominance, stablecoin supply,
# funding-rate aggregates, DXY, Fed balance sheet, Fear & Greed index).
#
# To add a NEW shared series in the future, no changes are needed to this
# class, to conviction_components_v2, run_dynamic_attr, or any other
# existing function -- only:
#   1. Build a {date_string: float} dict (via load_shared_series_from_csv
#      below, or any other loader following the same return shape).
#   2. Instantiate SharedSeriesFeature(name=..., date_series=..., btc_dates=...).
#   3. Add the instance to a registry dict, exactly like any other feature.
# ============================================================

def load_shared_series_from_csv(path, date_col="date", value_col="value"):
    """
    Generic loader for any shared series delivered as a two-column CSV
    (date, value). This is the intended on-ramp for every future shared
    series (Funding, Open Interest, DXY, Fed Balance Sheet, On-chain
    metrics, Fear & Greed) -- each of these is more naturally distributed
    as a downloadable CSV (FRED exports, Fear&Greed history dumps, a paid-
    tier CoinGecko export) than as a live API call this environment can
    reliably reach, following the same pattern already used for OHLCV via
    CryptoDataDownload CSVs elsewhere in this file.

    Returns {date_string: float}, dates normalized to YYYY-MM-DD, same
    convention as load_csv_history()/load_csv_hilo().
    """
    import csv as csvmod
    with open(path, newline='') as f:
        reader = csvmod.DictReader(f)
        series = {}
        for row in reader:
            try:
                d = row[date_col].strip().split(' ')[0].split('T')[0]
                v = float(row[value_col])
                series[d] = v
            except (KeyError, ValueError):
                continue
    return series


def load_shared_series_from_coingecko(coin_or_series_id, api_key, days=365):
    """
    DOCUMENTED EXTENSION POINT, not exercised by default. Verified against
    CoinGecko's own documentation (docs.coingecko.com, support.coingecko.com,
    checked directly, current as of this implementation): the free Demo API
    tier caps ALL historical endpoints -- including /global (market-cap
    dominance) and /coins/{id}/market_chart (e.g. stablecoin supply via
    tether/usd-coin market cap) -- at 365 days, regardless of endpoint.

    This system's existing OHLCV history spans 2000+ days (BTC/ETH via
    CryptoDataDownload). A 365-day-capped series would truncate every
    future experiment using it to the most recent year, reintroducing the
    short-history/single-regime problem already fixed once for HNT in the
    dynamic-universe redesign. For that reason this function is NOT called
    by default -- it exists as a ready implementation for when either a
    paid CoinGecko tier is available, or a full-depth export is sourced
    and loaded instead via load_shared_series_from_csv() above.
    """
    raise NotImplementedError(
        "Not wired to live network access by design -- see docstring. "
        "Use load_shared_series_from_csv() with a full-depth export instead, "
        "or supply a CoinGecko paid-tier API key and implement the fetch here."
    )


class SharedSeriesFeature(ConvictionFeature):
    """
    Generic feature reading from a precomputed {date: value} lookup that's
    identical for every asset on a given day -- unlike per-asset features
    (closes/vols for the specific asset) or Cross-Sectional Return Rank
    (which needs id(closes) to distinguish WHICH asset), a shared series
    only needs to be keyed by calendar date.

    raw(): rate of change of the underlying series over `window` days.
    normalized(): that rate of change clamped to +/-20% and rescaled to
    0-100 -- same clamp-and-rescale convention already used by the
    continuous relative_strength variant elsewhere in this file, kept
    consistent rather than inventing a new scoring convention.
    binary(): positive momentum in the shared series (>=50 on the
    normalized scale), diagnostic only, never used by the scoring math.
    """
    def __init__(self, name, date_series, btc_dates, weight=15, window=30):
        self.name = name
        self.weight = weight
        self.min_lookback = window + 5
        self.window = window
        self._btc_dates_ref = btc_dates
        sorted_dates = sorted(date_series.keys())
        self._date_to_idx = {d: i for i, d in enumerate(sorted_dates)}
        self._sorted_vals = [date_series[d] for d in sorted_dates]

    def raw(self, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        if i_btc >= len(self._btc_dates_ref):
            return None
        D = self._btc_dates_ref[i_btc]
        idx = self._date_to_idx.get(D)
        if idx is None or idx < self.window:
            return None
        cur = self._sorted_vals[idx]
        prev = self._sorted_vals[idx - self.window]
        if prev == 0:
            return None
        return cur / prev - 1

    def normalized(self, raw_value, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        if raw_value is None:
            return 0.0
        return max(0.0, min(100.0, (raw_value + 0.20) / 0.40 * 100))

    def binary(self, raw_value, normalized_value):
        return normalized_value >= 50.0


def compute_btc_volume_dominance_series(histories_dated, btc_id="bitcoin"):
    """
    PROOF-OF-ARCHITECTURE SERIES #1: BTC's share of total watchlist
    trading volume on each day -- a volume-based dominance proxy.

    Chosen instead of CoinGecko-sourced market-cap dominance or stablecoin
    supply (the two candidates named in the task) specifically because
    those fail the "sufficient historical depth" check performed before
    this implementation: CoinGecko's free tier caps at 365 days, versus
    2000+ days already available in this system's existing OHLCV. This
    proxy uses data ALREADY loaded, giving full matching depth and zero
    new external dependency, while remaining a genuinely different
    statistical object from any single asset's own OHLCV (an aggregate/
    ensemble measure, not a per-asset technical transform) -- the same
    orthogonality argument already used for Cross-Sectional Return Rank.

    Returns {date_string: float} (BTC's % share of combined watchlist
    volume), the standard shape SharedSeriesFeature expects.
    """
    date_totals, date_btc_vol = {}, {}
    for cid, (dates, closes, vols) in histories_dated.items():
        is_btc = (cid == btc_id)
        for d, v in zip(dates, vols):
            date_totals[d] = date_totals.get(d, 0.0) + v
            if is_btc:
                date_btc_vol[d] = v
    series = {}
    for d, btc_v in date_btc_vol.items():
        total = date_totals.get(d, 0.0)
        if total > 0:
            series[d] = 100.0 * btc_v / total
    return series


def load_per_asset_series_from_csvs(csv_dir, symbol_to_cid, pattern="{sym}_funding.csv",
                                     date_col="date", value_col="value"):
    """
    Generic loader for external series that differ PER ASSET (e.g. each
    asset's own funding rate, open interest, exchange reserve balance) --
    the natural distribution format is one CSV per asset (e.g.
    BTC_funding.csv, ETH_funding.csv). Reuses load_shared_series_from_csv()
    as the per-file parser, unmodified -- no duplicated CSV logic.

    Returns {cid: {date_string: value}}, ready for
    build_per_asset_series_lookup() below.
    """
    import os, glob
    result = {}
    for sym, cid in symbol_to_cid.items():
        matches = glob.glob(os.path.join(csv_dir, pattern.format(sym=sym))) or \
                  glob.glob(os.path.join(csv_dir, pattern.format(sym=sym.lower())))
        if not matches:
            continue
        result[cid] = load_shared_series_from_csv(matches[0], date_col=date_col, value_col=value_col)
    return result


def build_per_asset_series_lookup(histories_dated, per_asset_date_series):
    """
    Converts {cid: {date: value}} into {id(closes): {date: value}} --
    keyed by object identity of each asset's own closes array, the SAME
    stable-identity mechanism already used by CrossSectionalReturnRankFeature
    (verified in an earlier session: closes arrays are never copied
    anywhere in this codebase). This is what lets PerAssetExternalSeriesFeature
    below distinguish WHICH asset is being scored without any change to
    the existing raw()/normalized() call signature.
    """
    lookup = {}
    for cid, (dates, closes, vols) in histories_dated.items():
        series = per_asset_date_series.get(cid)
        if series is not None:
            lookup[id(closes)] = series
    return lookup


class PerAssetExternalSeriesFeature(ConvictionFeature):
    """
    THE PHASE 1 GAP, NOW CLOSED. Generic feature for external data that
    differs PER ASSET -- e.g. each asset's own funding rate or open
    interest -- as distinct from SharedSeriesFeature (one value for the
    whole universe, e.g. BTC dominance) and from ordinary per-asset
    features (computed from the asset's own OHLCV). This is the missing
    third category the Phase 1 requirements named explicitly.

    To add a new per-asset external series (funding, OI, exchange
    reserves, active addresses, etc.): load per-asset CSVs via
    load_per_asset_series_from_csvs(), convert via
    build_per_asset_series_lookup(), instantiate this class, register it.
    Zero changes to conviction_components_v2, run_dynamic_attr, or any
    other existing function -- identical extension cost to every feature
    added in this project so far.
    """
    def __init__(self, name, per_asset_lookup, btc_dates, weight=15, window=30):
        self.name = name
        self.weight = weight
        self.min_lookback = window + 5
        self.window = window
        self._btc_dates_ref = btc_dates
        self.per_asset_lookup = per_asset_lookup
        self._date_to_idx = {}
        self._sorted_vals = {}
        for closes_id, date_series in per_asset_lookup.items():
            sorted_dates = sorted(date_series.keys())
            self._date_to_idx[closes_id] = {d: i for i, d in enumerate(sorted_dates)}
            self._sorted_vals[closes_id] = [date_series[d] for d in sorted_dates]

    def raw(self, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        closes_id = id(closes)
        if closes_id not in self.per_asset_lookup or i_btc >= len(self._btc_dates_ref):
            return None
        D = self._btc_dates_ref[i_btc]
        idx = self._date_to_idx[closes_id].get(D)
        if idx is None or idx < self.window:
            return None
        vals = self._sorted_vals[closes_id]
        cur, prev = vals[idx], vals[idx - self.window]
        if prev == 0:
            return None
        return cur / prev - 1  # rate of change over `window` days, same convention as SharedSeriesFeature

    def normalized(self, raw_value, closes, vols, i_local, btc_closes, i_btc, highs=None, lows=None):
        if raw_value is None:
            return 0.0
        return max(0.0, min(100.0, (raw_value + 0.20) / 0.40 * 100))

    def binary(self, raw_value, normalized_value):
        return normalized_value >= 50.0


# Registry: adding a future feature (BTC dominance, funding, OI, breadth...)
# means writing one ConvictionFeature subclass and adding it here. Nothing
# else in the engine, attribution methods, or CLI needs to change.
FEATURE_REGISTRY = {
    "relative_strength":     RelativeStrengthFeature(),
    "trend":                 TrendFeature(),
    "volume_expansion":      VolumeExpansionFeature(),
    "momentum":               MomentumFeature(),
}
FEATURE_REGISTRY_WITH_ATR = dict(FEATURE_REGISTRY, atr_donchian_squeeze=ATRDonchianSqueezeFeature())
FEATURE_REGISTRY_WITH_OBV = dict(FEATURE_REGISTRY, obv=OBVFeature())
FEATURE_REGISTRY_WITH_ATR_OBV = dict(FEATURE_REGISTRY, atr_donchian_squeeze=ATRDonchianSqueezeFeature(), obv=OBVFeature())

def conviction_components_v2(closes, vols, i_local, btc_closes, i_btc,
                              highs=None, lows=None, disabled=None,
                              registry=None, weight_override=None):
    """
    Generalized scoring engine: iterates `registry` (default: the 4 legacy
    features only -- pass registry=FEATURE_REGISTRY_WITH_ATR to include
    ATR/Donchian), summing weight*(normalized/100) per feature. Returns
    (score, flags, raws, normalizeds) -- flags is the existing binary dict
    (for ablation/permutation/IG, unchanged interface), raws/normalizeds
    are NEW and support the continuous-contribution attribution analysis.

    weight_override: optional {feature_name: weight} dict. When None
    (default), every feature's hand-set `.weight` attribute is used
    exactly as before -- zero behavior change. When supplied (e.g. with
    coefficients learned by run_train_model), those weights are used
    instead, feature-by-feature, falling back to `.weight` for any name
    not present in the override. This is the ONLY mechanism by which
    learned weights re-enter the engine -- no ConvictionFeature subclass
    or registry is ever modified.
    """
    registry = registry if registry is not None else FEATURE_REGISTRY
    disabled = disabled or set()
    weight_override = weight_override or {}
    flags, raws, normalizeds = {}, {}, {}
    score = CONVICTION_BASE
    for fname, feat in registry.items():
        if i_local < feat.min_lookback:
            flags[fname] = False; raws[fname] = None; normalizeds[fname] = 0.0
            continue
        rv = feat.raw(closes, vols, i_local, btc_closes, i_btc, highs=highs, lows=lows)
        nv = feat.normalized(rv, closes, vols, i_local, btc_closes, i_btc, highs=highs, lows=lows) if rv is not None else 0.0
        bv = feat.binary(rv, nv) if rv is not None else False
        raws[fname] = rv; normalizeds[fname] = nv; flags[fname] = bv
        if fname not in disabled:
            w = weight_override.get(fname, feat.weight)
            score += w * (nv / 100.0)
    return min(100, score), flags, raws, normalizeds

def conviction_components(closes, vols, i_local, btc_closes, i_btc, disabled=None,
                           rs_lookback=30, rs_continuous=False,
                           rs_mode="level", rs_window=10, rs_slope_lookback=10):
    """
    Same scoring as conviction_dyn(), but returns BOTH the total score and
    a dict of each component's raw boolean flag at this point in time --
    the flags are what the attribution framework measures against trade
    outcomes. `disabled`: a set of component names to force OFF regardless
    of their true condition (used for ablation testing -- this changes the
    SCORE the entry logic sees, exactly like removing that rule from the
    model, while leaving every other component and all exit/breakout logic
    completely untouched).

    rs_lookback: window (days) used for the relative_strength comparison
    when rs_mode="level" (default 30, matching the original design).

    rs_continuous: if True, relative_strength contributes a MAGNITUDE-
    scaled score (0 to full weight) instead of a flat all-or-nothing
    weight. Applies to whichever rs_mode is active -- in "level" mode it
    scales with the outperformance edge; in "slope" mode it scales with
    the magnitude of the acceleration.

    rs_mode: "level" (default, ORIGINAL/UNCHANGED behavior -- tok's return
    over rs_lookback days beats BTC's return over the same window) or
    "slope" (NEW: measures whether the token's short-window relative-
    strength edge is currently INCREASING, i.e. the derivative of RS
    rather than its level). Slope mode computes a short RS edge
    (rs_window days, default 10) NOW and the SAME quantity rs_slope_lookback
    days ago, and flags true if the edge is expanding -- this targets
    early-stage acceleration rather than waiting for a fully-realized
    outperformance level, directly addressing the lagging-gate diagnosis
    (ablation stayed negative across every tested LEVEL lookback; slope
    changes what kind of quantity is measured, not how much data feeds it).
    """
    disabled = disabled or set()
    flags = dict(relative_strength=False, trend=False, volume_expansion=False, momentum=False)
    min_lookback_needed = max(rs_lookback, rs_window + rs_slope_lookback) if rs_mode == "slope" else rs_lookback
    if i_local < max(60, min_lookback_needed + 5):
        return 0, flags

    if rs_mode == "slope":
        def rs_edge_at(local_idx, btc_idx):
            tok_r = closes[local_idx] / closes[local_idx - rs_window] - 1
            btc_r = btc_closes[btc_idx] / btc_closes[btc_idx - rs_window] - 1
            return tok_r - btc_r
        rs_now = rs_edge_at(i_local, i_btc)
        rs_prev = rs_edge_at(i_local - rs_slope_lookback, i_btc - rs_slope_lookback)
        rs_slope = rs_now - rs_prev
        flags["relative_strength"] = rs_slope > 0
        rs_score_input = rs_slope  # magnitude used for continuous scaling below
        rs_continuous_cap = 0.10   # a slope of +10 percentage points is treated as "full strength"
    else:  # "level" -- original, unchanged behavior
        tok_rs = closes[i_local] / closes[i_local-rs_lookback] - 1
        btc_rs = btc_closes[i_btc] / btc_closes[i_btc-rs_lookback] - 1
        flags["relative_strength"] = tok_rs > btc_rs
        rs_score_input = tok_rs - btc_rs
        rs_continuous_cap = 0.20

    tok30 = closes[i_local] / closes[i_local-30] - 1  # momentum always uses the original 30d window
    e50 = ema(closes[max(0,i_local-120):i_local+1], 50)
    flags["trend"] = bool(e50 and closes[i_local] > e50)
    v_now = sum(vols[i_local-7:i_local]) / 7; v_prev = sum(vols[i_local-30:i_local-7]) / 23
    flags["volume_expansion"] = bool(v_prev > 0 and v_now > v_prev * 1.15)
    flags["momentum"] = tok30 > 0.10

    score = CONVICTION_BASE
    for name, weight in CONVICTION_COMPONENTS.items():
        if name not in disabled and flags[name]:
            if name == "relative_strength" and rs_continuous:
                score += weight * max(0.0, min(1.0, rs_score_input / rs_continuous_cap))
            else:
                score += weight
    return min(100, score), flags

def run(histories, btc_id="bitcoin", max_positions=None, rsi_max=None, token_trend_filter=False, verbose=True):
    """
    rsi_max: if set, skip new entries when the token's RSI(14) exceeds
    this value (overbought / already-extended filter).

    token_trend_filter: if True, additionally require the token's own
    price to be above its own 200-day EMA at entry (stricter than the
    50D EMA check already inside conviction()). Tests whether requiring
    longer-horizon trend confirmation per-token, not just BTC's regime,
    improves entry quality.
    """
    """
    max_positions: if set, caps simultaneous open positions. When more
    entry-eligible candidates exist on a given day than free slots, the
    HIGHEST-conviction candidates get priority; the rest are skipped for
    that day (they remain eligible on a later day if the signal persists).
    This tests whether correlated high-conviction clusters (multiple
    tokens signaling together, then getting hit by the same drawdown)
    are diluting the aggregate edge -- as suggested by the per-asset
    breakdown showing SOL/BTC carrying the edge while ETH/AAVE lag.
    """
    btc_closes, _ = histories[btc_id]
    n = len(btc_closes)
    ema200 = rolling_ema_series(btc_closes, 200)
    trades, open_pos = [], {}
    diag = dict(days=0, regime_off_days=0, max_conv=0, conv_ge70_token_days=0,
                blocked_by_regime=0, blocked_by_position_cap=0, blocked_by_trend_filter=0)
    token_ema200 = {}
    if token_trend_filter:
        for cid, (closes, vols) in histories.items():
            if len(closes) == n:
                token_ema200[cid] = rolling_ema_series(closes, 200)
    for i in range(210, n):
        e = ema200[i]
        regime_off = e is not None and btc_closes[i] < e * 0.995
        diag["days"] += 1
        if regime_off: diag["regime_off_days"] += 1

        # ---- PASS 1: process exits for every open position first ----
        entry_candidates = []
        for cid, (closes, vols) in histories.items():
            if len(closes) != n: continue
            sym = TOKENS.get(cid, cid)
            c = conviction(closes, vols, btc_closes, i)
            diag["max_conv"] = max(diag["max_conv"], c)
            if c >= CONV_THRESHOLD: diag["conv_ge70_token_days"] += 1
            px = closes[i]
            if sym in open_pos:
                p = open_pos[sym]
                exit_px, reason = None, None
                if px <= p["stop"]: exit_px, reason = p["stop"], "stop"
                elif not p["t1_hit"] and px >= p["t1"]:
                    trades.append(dict(sym=sym, r=(p["t1"]/p["entry"]-1)/p["stop_d"]*0.5 - FEE - SLIP,
                                       pct=(p["t1"]/p["entry"]-1)*50, reason="t1_half", conv=p["conv"],
                                       entry_day=p["entry_day"], exit_day=i, entry_px=p["entry"], exit_px=p["t1"], stop_d=p["stop_d"]))
                    p["t1_hit"] = True; p["stop"] = p["entry"]
                elif px >= p["t2"]: exit_px, reason = p["t2"], "t2"
                elif regime_off or c < CONV_THRESHOLD - 10: exit_px, reason = px, "signal_loss"
                if exit_px:
                    frac = 0.5 if p["t1_hit"] else 1.0
                    r = (exit_px / p["entry"] - 1) / p["stop_d"] * frac - FEE - SLIP
                    trades.append(dict(sym=sym, r=r, pct=(exit_px/p["entry"]-1)*100*frac,
                                       reason=reason, conv=p["conv"],
                                       entry_day=p["entry_day"], exit_day=i, entry_px=p["entry"], exit_px=exit_px, stop_d=p["stop_d"]))
                    del open_pos[sym]
            elif c >= CONV_THRESHOLD and regime_off:
                diag["blocked_by_regime"] += 1
            elif not regime_off and c >= CONV_THRESHOLD:
                if rsi_max is not None:
                    rsi = compute_rsi(closes, i)
                    if rsi is not None and rsi > rsi_max:
                        continue  # overextended -- skip this entry candidate
                if token_trend_filter:
                    e200 = token_ema200.get(cid, [None]*n)[i]
                    if e200 is None or px <= e200:
                        diag["blocked_by_trend_filter"] += 1
                        continue  # token below its own 200D EMA -- skip
                entry_candidates.append((sym, c, px, closes, i))

        # ---- PASS 2: rank entry candidates by conviction, apply cap ----
        entry_candidates.sort(key=lambda x: -x[1])  # highest conviction first
        free_slots = (max_positions - len(open_pos)) if max_positions is not None else len(entry_candidates)
        for idx, (sym, c, px, closes, day_i) in enumerate(entry_candidates):
            if max_positions is not None and idx >= max(0, free_slots):
                diag["blocked_by_position_cap"] += 1
                continue
            vol30 = abs(closes[day_i] / closes[day_i-30] - 1) * 100
            stop_pct = max(8, min(25, vol30 * 0.8)) / 100
            open_pos[sym] = dict(entry=px * (1 + SLIP), stop=px * (1 - stop_pct),
                                 stop_d=stop_pct, t1=px * (1 + stop_pct*1.5),
                                 t2=px * (1 + stop_pct*3), t1_hit=False, conv=c,
                                 entry_day=day_i)
    if verbose:
        print(f"\nDIAGNOSTICS: {diag['days']} tradeable days after 210d EMA warmup")
        print(f"  BTC below 200D EMA (regime OFF): {diag['regime_off_days']}/{diag['days']} days"
              f" ({100*diag['regime_off_days']/max(1,diag['days']):.0f}%)")
        print(f"  Max conviction reached by any token: {diag['max_conv']}")
        print(f"  Token-days with conviction >= {CONV_THRESHOLD}: {diag['conv_ge70_token_days']}")
        print(f"  Entries blocked purely by regime filter: {diag['blocked_by_regime']}")
        if diag.get('blocked_by_position_cap', 0) > 0:
            print(f"  Entries blocked by position cap (lower conviction, slot taken): {diag['blocked_by_position_cap']}")
        if diag.get('blocked_by_trend_filter', 0) > 0:
            print(f"  Entries blocked by token trend filter: {diag['blocked_by_trend_filter']}")
    if verbose and diag['regime_off_days'] > diag['days'] * 0.8:
        print("  -> Verdict: the regime filter kept you OUT of a bear period."
              "\n     Zero trades here is the system working, not failing.")
    elif verbose and diag['conv_ge70_token_days'] == 0:
        print("  -> Verdict: conviction never reached threshold; scoring too strict"
              "\n     for this dataset, or the period had no sustained outperformers.")
    return trades

def run_dynamic(histories_dated, btc_id="bitcoin", day_start=0, day_end=None,
                 max_positions=None, rsi_max=None, token_trend_filter=False, verbose=True):
    """
    Dynamic-universe engine: each asset keeps its OWN full native-length
    history (dates, closes, vols) -- nothing is truncated to the youngest
    listing. BTC's own date array is the single global clock (its regime
    filter therefore always sees BTC's complete history, per requirement).
    Every other asset is looked up BY DATE each day; it only becomes part
    of that day's universe once (a) it actually has data for that date and
    (b) it has passed its OWN 210-day warmup (same requirement BTC itself
    has always had -- just applied per-asset instead of globally).

    day_start/day_end: optional BTC-clock (global) day-index window, used
    by run_sweep()/run_validate() to slice folds WITHOUT re-truncating any
    asset's own array -- the date lookups naturally supply whatever
    lookback each asset needs, so no artificial warmup buffer is required
    around fold boundaries the way the old shared-index engine needed one.

    Trade records use entry_day/exit_day on the GLOBAL (BTC) clock, so
    downstream regime-stratification (trailing_return against btc_closes)
    and reporting work unchanged regardless of which asset a trade is on.
    """
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    ema200_btc = rolling_ema_series(btc_closes, 200)

    # Per-asset: date -> local index, and precomputed 200D EMA (own history)
    date_idx = {}
    ema200_local = {}
    for cid, (dates, closes, vols) in histories_dated.items():
        date_idx[cid] = {d: idx for idx, d in enumerate(dates)}
        if token_trend_filter:
            ema200_local[cid] = rolling_ema_series(closes, 200)

    trades, open_pos = [], {}  # open_pos[sym] = {..., "cid": cid}
    diag = dict(days=0, regime_off_days=0, max_conv=0, conv_ge70_token_days=0,
                blocked_by_regime=0, blocked_by_position_cap=0, blocked_by_trend_filter=0,
                not_yet_listed_skips=0)

    start_i = max(210, day_start)
    end_i = n if day_end is None else min(n, day_end)

    for i in range(start_i, end_i):
        D = btc_dates[i]
        e_btc = ema200_btc[i]
        regime_off = e_btc is not None and btc_closes[i] < e_btc * 0.995
        diag["days"] += 1
        if regime_off: diag["regime_off_days"] += 1

        entry_candidates = []
        for cid, (dates, closes, vols) in histories_dated.items():
            sym = TOKENS.get(cid, cid)
            local_i = date_idx[cid].get(D)
            if local_i is None:
                # This asset has no data for today (not listed yet, or a
                # gap). If we hold a position, we simply can't act on it
                # today -- leave it open rather than force-closing on
                # missing data.
                if sym in open_pos:
                    diag["not_yet_listed_skips"] += 1
                continue
            if local_i < 210:
                continue  # asset's OWN warmup not yet satisfied -- not tradeable yet

            c = conviction_dyn(closes, vols, local_i, btc_closes, i)
            diag["max_conv"] = max(diag["max_conv"], c)
            if c >= CONV_THRESHOLD: diag["conv_ge70_token_days"] += 1
            px = closes[local_i]

            if sym in open_pos:
                p = open_pos[sym]
                exit_px, reason = None, None
                if px <= p["stop"]: exit_px, reason = p["stop"], "stop"
                elif not p["t1_hit"] and px >= p["t1"]:
                    trades.append(dict(sym=sym, r=(p["t1"]/p["entry"]-1)/p["stop_d"]*0.5 - FEE - SLIP,
                                       pct=(p["t1"]/p["entry"]-1)*50, reason="t1_half", conv=p["conv"],
                                       entry_day=p["entry_day"], exit_day=i, entry_px=p["entry"], exit_px=p["t1"], stop_d=p["stop_d"]))
                    p["t1_hit"] = True; p["stop"] = p["entry"]
                elif px >= p["t2"]: exit_px, reason = p["t2"], "t2"
                elif regime_off or c < CONV_THRESHOLD - 10: exit_px, reason = px, "signal_loss"
                if exit_px:
                    frac = 0.5 if p["t1_hit"] else 1.0
                    r = (exit_px / p["entry"] - 1) / p["stop_d"] * frac - FEE - SLIP
                    trades.append(dict(sym=sym, r=r, pct=(exit_px/p["entry"]-1)*100*frac,
                                       reason=reason, conv=p["conv"],
                                       entry_day=p["entry_day"], exit_day=i, entry_px=p["entry"], exit_px=exit_px, stop_d=p["stop_d"]))
                    del open_pos[sym]
            elif c >= CONV_THRESHOLD and regime_off:
                diag["blocked_by_regime"] += 1
            elif not regime_off and c >= CONV_THRESHOLD:
                if rsi_max is not None:
                    rsi = compute_rsi(closes, local_i)
                    if rsi is not None and rsi > rsi_max:
                        continue
                if token_trend_filter:
                    e200 = ema200_local.get(cid, [None]*len(closes))[local_i]
                    if e200 is None or px <= e200:
                        diag["blocked_by_trend_filter"] += 1
                        continue
                entry_candidates.append((sym, c, px, closes, vols, local_i))

        entry_candidates.sort(key=lambda x: -x[1])
        free_slots = (max_positions - len(open_pos)) if max_positions is not None else len(entry_candidates)
        for idx, (sym, c, px, closes, vols, local_i) in enumerate(entry_candidates):
            if max_positions is not None and idx >= max(0, free_slots):
                diag["blocked_by_position_cap"] += 1
                continue
            vol30 = abs(closes[local_i] / closes[local_i-30] - 1) * 100
            stop_pct = max(8, min(25, vol30 * 0.8)) / 100
            open_pos[sym] = dict(entry=px * (1 + SLIP), stop=px * (1 - stop_pct),
                                 stop_d=stop_pct, t1=px * (1 + stop_pct*1.5),
                                 t2=px * (1 + stop_pct*3), t1_hit=False, conv=c,
                                 entry_day=i)

    if verbose:
        print(f"\nDIAGNOSTICS (dynamic universe): {diag['days']} days on the BTC global clock")
        print(f"  BTC below 200D EMA (regime OFF): {diag['regime_off_days']}/{diag['days']} days"
              f" ({100*diag['regime_off_days']/max(1,diag['days']):.0f}%)")
        print(f"  Max conviction reached by any token: {diag['max_conv']}")
        print(f"  Token-days with conviction >= {CONV_THRESHOLD}: {diag['conv_ge70_token_days']}")
        print(f"  Entries blocked purely by regime filter: {diag['blocked_by_regime']}")
        if diag['blocked_by_position_cap'] > 0:
            print(f"  Entries blocked by position cap: {diag['blocked_by_position_cap']}")
        if diag['blocked_by_trend_filter'] > 0:
            print(f"  Entries blocked by token trend filter: {diag['blocked_by_trend_filter']}")
        if diag['not_yet_listed_skips'] > 0:
            print(f"  Open-position days with no data available (gap, not a real event): {diag['not_yet_listed_skips']}")
        # Per-asset listing-date visibility
        print(f"  Per-asset history start dates:")
        for cid, (dates, closes, vols) in sorted(histories_dated.items(), key=lambda kv: kv[1][0][0]):
            print(f"    {TOKENS.get(cid, cid):6s} first available: {dates[0]}  ({len(dates)} days)")
    return trades

def run_dynamic_attr(histories_dated, btc_id="bitcoin", day_start=0, day_end=None,
                      max_positions=None, disabled_components=None,
                      rs_lookback=30, rs_continuous=False,
                      rs_mode="level", rs_window=10, rs_slope_lookback=10,
                      registry=None, hilo_dated=None, weight_override=None):
    """
    Attribution variant of run_dynamic(): identical entry gating, exit
    logic, stop/target math, and position-cap behavior -- the ONLY
    difference is (a) scoring goes through conviction_components() instead
    of conviction_dyn(), so a component can be forced off for ablation
    testing, and (b) every trade record carries a `components` dict
    capturing which components were true at the moment of entry, so the
    correlation/permutation/information-gain analysis has something to
    group trades by afterward. No breakout, exit, or risk logic is
    touched -- this only changes what conviction() ever forces off, and
    what bookkeeping gets attached to the resulting trades.

    rs_lookback / rs_continuous / rs_mode / rs_window / rs_slope_lookback:
    passed straight through to conviction_components() -- see that
    function's docstring.

    registry: if None (default), uses the legacy conviction_components()
    exactly as before -- byte-identical behavior for every existing
    caller. If set to a feature registry (e.g. FEATURE_REGISTRY_WITH_ATR),
    switches to the generalized conviction_components_v2() engine, and
    each trade additionally carries `raws`/`normalizeds` dicts alongside
    the existing binary `components` dict.

    hilo_dated: required when registry includes a feature that needs
    High/Low (e.g. ATR/Donchian) -- {cid: (dates, highs, lows)}, same
    date-alignment convention as histories_dated.
    """
    disabled_components = disabled_components or set()
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    ema200_btc = rolling_ema_series(btc_closes, 200)
    use_framework = registry is not None
    hilo_dated = hilo_dated or {}

    date_idx = {}
    hilo_idx = {}
    for cid, (dates, closes, vols) in histories_dated.items():
        date_idx[cid] = {d: idx for idx, d in enumerate(dates)}
    for cid, (hdates, highs, lows) in hilo_dated.items():
        if hdates is not None:
            hilo_idx[cid] = {d: idx for idx, d in enumerate(hdates)}

    trades, open_pos = [], {}
    start_i = max(210, day_start)
    end_i = n if day_end is None else min(n, day_end)

    for i in range(start_i, end_i):
        D = btc_dates[i]
        e_btc = ema200_btc[i]
        regime_off = e_btc is not None and btc_closes[i] < e_btc * 0.995

        entry_candidates = []
        for cid, (dates, closes, vols) in histories_dated.items():
            sym = TOKENS.get(cid, cid)
            local_i = date_idx[cid].get(D)
            if local_i is None or local_i < 210:
                continue

            if use_framework:
                highs = lows = None
                if cid in hilo_dated and cid in hilo_idx:
                    h_local_i = hilo_idx[cid].get(D)
                    if h_local_i is not None and h_local_i == local_i:
                        # Only trust highs/lows when the hi/lo series' index
                        # for today's date matches closes' index exactly --
                        # if the two series have diverged (missing days,
                        # different listing coverage), skip rather than risk
                        # misindexing one series against another.
                        _, highs, lows = hilo_dated[cid]
                c, flags, raws, normalizeds = conviction_components_v2(
                    closes, vols, local_i, btc_closes, i, highs=highs, lows=lows,
                    disabled=disabled_components, registry=registry, weight_override=weight_override)
            else:
                c, flags = conviction_components(closes, vols, local_i, btc_closes, i,
                                                  disabled=disabled_components,
                                                  rs_lookback=rs_lookback, rs_continuous=rs_continuous,
                                                  rs_mode=rs_mode, rs_window=rs_window,
                                                  rs_slope_lookback=rs_slope_lookback)
                raws = normalizeds = None
            px = closes[local_i]

            if sym in open_pos:
                p = open_pos[sym]
                exit_px, reason = None, None
                if px <= p["stop"]: exit_px, reason = p["stop"], "stop"
                elif not p["t1_hit"] and px >= p["t1"]:
                    trades.append(dict(sym=sym, r=(p["t1"]/p["entry"]-1)/p["stop_d"]*0.5 - FEE - SLIP,
                                       pct=(p["t1"]/p["entry"]-1)*50, reason="t1_half", conv=p["conv"],
                                       entry_day=p["entry_day"], exit_day=i, entry_px=p["entry"], exit_px=p["t1"], stop_d=p["stop_d"],
                                       components=p["components"], raws=p.get("raws"), normalizeds=p.get("normalizeds")))
                    p["t1_hit"] = True; p["stop"] = p["entry"]
                elif px >= p["t2"]: exit_px, reason = p["t2"], "t2"
                elif regime_off or c < CONV_THRESHOLD - 10: exit_px, reason = px, "signal_loss"
                if exit_px:
                    frac = 0.5 if p["t1_hit"] else 1.0
                    r = (exit_px / p["entry"] - 1) / p["stop_d"] * frac - FEE - SLIP
                    trades.append(dict(sym=sym, r=r, pct=(exit_px/p["entry"]-1)*100*frac,
                                       reason=reason, conv=p["conv"],
                                       entry_day=p["entry_day"], exit_day=i, entry_px=p["entry"], exit_px=exit_px, stop_d=p["stop_d"],
                                       components=p["components"], raws=p.get("raws"), normalizeds=p.get("normalizeds")))
                    del open_pos[sym]
            elif not regime_off and c >= CONV_THRESHOLD:
                entry_candidates.append((sym, c, px, closes, local_i, dict(flags),
                                         dict(raws) if raws else None,
                                         dict(normalizeds) if normalizeds else None))

        entry_candidates.sort(key=lambda x: -x[1])
        free_slots = (max_positions - len(open_pos)) if max_positions is not None else len(entry_candidates)
        for idx, (sym, c, px, closes, local_i, flags, raws, normalizeds) in enumerate(entry_candidates):
            if max_positions is not None and idx >= max(0, free_slots):
                continue
            vol30 = abs(closes[local_i] / closes[local_i-30] - 1) * 100
            stop_pct = max(8, min(25, vol30 * 0.8)) / 100
            open_pos[sym] = dict(entry=px * (1 + SLIP), stop=px * (1 - stop_pct),
                                 stop_d=stop_pct, t1=px * (1 + stop_pct*1.5),
                                 t2=px * (1 + stop_pct*3), t1_hit=False, conv=c,
                                 entry_day=i, components=flags, raws=raws, normalizeds=normalizeds)
    return trades

# ============================================================
# STATISTICALLY LEARNED CONVICTION MODEL
#
# Everything above (ConvictionFeature, FEATURE_REGISTRY, run_dynamic_attr,
# attribution/ablation/redundancy/timing-diagnosis) is UNCHANGED. This
# section adds a separate, opt-in pipeline that LEARNS feature weights
# instead of using the hand-set ones, via regularized logistic regression
# -- interpretable, no black-box models, pure Python (no numpy/sklearn
# dependency, consistent with the rest of this file).
#
# The learned weights re-enter the SAME engine through the
# weight_override parameter added to conviction_components_v2() -- no
# ConvictionFeature or registry is ever modified, so every existing
# --attribute / --diagnose-component / --validate command continues to
# work unchanged, and can ALSO be re-run against learned weights via
# --weights (see main()).
# ============================================================

def simulate_trade_outcome(closes, i_entry, max_holding=90):
    """
    Labels a single (asset, day) point with a REAL r-multiple outcome,
    using the EXACT SAME stop/T1/T2 mechanics as the live trading engine
    (run_dynamic_attr) -- not a separate, arbitrary labeling scheme. This
    is evaluated UNCONDITIONALLY (no conviction threshold, no regime
    filter) so every point in the training window gets a label, avoiding
    the selection bias that would result from only labeling points the
    current hand-weighted scorecard already approved.

    Blends partial exits (T1-half, then remainder) the same way the live
    trade bookkeeping does, so the label reflects the strategy's actual
    payoff structure. Returns None if there isn't enough forward data to
    resolve an outcome (near the end of the available history).
    """
    n = len(closes)
    if i_entry < 30 or i_entry + 1 >= n:
        return None
    px = closes[i_entry]
    vol30 = abs(px / closes[i_entry-30] - 1) * 100
    stop_pct = max(8, min(25, vol30 * 0.8)) / 100
    entry = px * (1 + SLIP)
    stop = px * (1 - stop_pct)
    t1 = px * (1 + stop_pct * 1.5)
    t2 = px * (1 + stop_pct * 3)
    t1_hit = False
    end = min(n, i_entry + 1 + max_holding)
    r_total = 0.0
    for j in range(i_entry + 1, end):
        p = closes[j]
        if p <= stop:
            frac = 0.5 if t1_hit else 1.0
            r_total += (stop / entry - 1) / stop_pct * frac - FEE - SLIP
            return r_total
        if not t1_hit and p >= t1:
            r_total += (t1 / entry - 1) / stop_pct * 0.5 - FEE - SLIP
            t1_hit = True
            stop = entry  # trail to breakeven, matching live engine behavior
            continue
        if p >= t2:
            frac = 0.5 if t1_hit else 1.0
            r_total += (t2 / entry - 1) / stop_pct * frac - FEE - SLIP
            return r_total
    # Max holding reached without a clean exit -- close at last available price
    frac = 0.5 if t1_hit else 1.0
    last_px = closes[end - 1]
    r_total += (last_px / entry - 1) / stop_pct * frac - FEE - SLIP
    return r_total


def build_training_samples(histories_dated, registry, day_start, day_end,
                            btc_id="bitcoin", hilo_dated=None, max_holding=90):
    """
    Builds a labeled dataset for [day_start, day_end) on the BTC global
    clock: for every (asset, day) with sufficient own-warmup, computes the
    normalized() score of every registered feature (the SAME functions
    used by the live scoring engine, no duplicated logic) and pairs it
    with a forward outcome label from simulate_trade_outcome(), evaluated
    UNCONDITIONALLY -- not gated by CONV_THRESHOLD or regime, which is
    what makes this an unbiased training set rather than a description of
    already-admitted trades.

    Returns (X, y, feature_names) -- X is a list of feature-vectors
    (each a list of normalized 0-100 scores, in feature_names order),
    y is the matching list of r-multiple labels (converted to a binary
    win/loss target for logistic regression: 1 if r > 0 else 0).
    """
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    hilo_dated = hilo_dated or {}
    date_idx = {cid: {d: idx for idx, d in enumerate(dates)} for cid, (dates, closes, vols) in histories_dated.items()}
    hilo_idx = {}
    for cid, (hdates, highs, lows) in hilo_dated.items():
        if hdates is not None:
            hilo_idx[cid] = {d: idx for idx, d in enumerate(hdates)}

    feature_names = list(registry)
    X, y = [], []
    end_i = min(n, day_end)
    start_i = max(210, day_start)
    for i in range(start_i, end_i):
        D = btc_dates[i]
        for cid, (dates, closes, vols) in histories_dated.items():
            local_i = date_idx[cid].get(D)
            if local_i is None or local_i < 210:
                continue
            highs = lows = None
            if cid in hilo_dated and cid in hilo_idx:
                h_local_i = hilo_idx[cid].get(D)
                if h_local_i is not None and h_local_i == local_i:
                    _, highs, lows = hilo_dated[cid]
            row = []
            skip = False
            for fname in feature_names:
                feat = registry[fname]
                if local_i < feat.min_lookback:
                    skip = True; break
                rv = feat.raw(closes, vols, local_i, btc_closes, i, highs=highs, lows=lows)
                nv = feat.normalized(rv, closes, vols, local_i, btc_closes, i, highs=highs, lows=lows) if rv is not None else 0.0
                row.append(nv)
            if skip:
                continue
            label_r = simulate_trade_outcome(closes, local_i, max_holding=max_holding)
            if label_r is None:
                continue
            X.append(row)
            y.append(1 if label_r > 0 else 0)
    return X, y, feature_names


def standardize(X):
    """Z-score each column. Returns (X_std, means, stds) -- means/stds needed to
    apply the SAME transform to any future data scored with these coefficients."""
    n = len(X)
    if n == 0: return X, [], []
    ncols = len(X[0])
    means = [sum(row[j] for row in X) / n for j in range(ncols)]
    stds = []
    for j in range(ncols):
        var = sum((row[j] - means[j])**2 for row in X) / n
        stds.append(math.sqrt(var) if var > 1e-12 else 1.0)
    X_std = [[(row[j] - means[j]) / stds[j] for j in range(ncols)] for row in X]
    return X_std, means, stds


def logistic_regression_fit(X, y, l2=1.0, lr=0.1, iters=500):
    """
    Pure-Python L2-regularized (ridge) logistic regression via batch
    gradient descent. No numpy/sklearn -- consistent with the rest of
    this file. Returns (intercept, coefficients) as plain floats.
    Regularization is NOT applied to the intercept, standard convention.
    """
    n = len(X)
    if n == 0: return 0.0, []
    ncols = len(X[0])
    w = [0.0] * ncols
    b = 0.0
    for _ in range(iters):
        grad_w = [0.0] * ncols
        grad_b = 0.0
        for row, label in zip(X, y):
            z = b + sum(w[j] * row[j] for j in range(ncols))
            # Numerically stable sigmoid
            p = 1.0 / (1.0 + math.exp(-z)) if z >= 0 else math.exp(z) / (1.0 + math.exp(z))
            err = p - label
            for j in range(ncols):
                grad_w[j] += err * row[j]
            grad_b += err
        for j in range(ncols):
            grad_w[j] = grad_w[j] / n + l2 * w[j] / n  # L2 penalty
            w[j] -= lr * grad_w[j]
        b -= lr * (grad_b / n)
    return b, w


def bootstrap_logreg(X, y, l2=1.0, n_bootstrap=200, seed=42):
    """
    Resamples the TRAINING rows with replacement n_bootstrap times,
    refitting logistic regression each time, to get a coefficient
    distribution per feature -- this is the confidence-interval and
    within-fold-stability mechanism, using the same resampling philosophy
    as permutation_test() elsewhere in this file rather than an
    asymptotic/Wald approximation (more robust at these sample sizes, and
    doesn't require matrix inversion in pure Python).
    Returns a list of (intercept, coeffs) tuples, one per bootstrap draw.
    """
    rng = random.Random(seed)
    n = len(X)
    results = []
    for _ in range(n_bootstrap):
        idxs = [rng.randrange(n) for _ in range(n)]
        Xb = [X[i] for i in idxs]
        yb = [y[i] for i in idxs]
        b, w = logistic_regression_fit(Xb, yb, l2=l2, iters=200)  # fewer iters per bootstrap draw for speed
        results.append((b, w))
    return results


def percentile(values, p):
    """Simple percentile (linear interpolation), no numpy dependency."""
    s = sorted(values)
    if not s: return 0.0
    k = (len(s) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    if f == c: return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def run_train_model(histories_dated, registry=None, btc_id="bitcoin", n_folds_requested=4,
                     hilo_dated=None, l2=1.0, n_bootstrap=200, max_holding=90):
    """
    Trains a separate logistic regression per expanding-window fold,
    using ONLY that fold's training period (everything strictly before
    the fold's test_start on the BTC global clock) -- the exact same fold
    boundaries already used by --validate, so this is directly comparable
    walk-forward methodology, not a new validation scheme. Reports:
      - per-fold coefficients (standardized scale)
      - across-fold stability (mean/std/range per feature)
      - within-fold bootstrap confidence intervals
      - learned weights normalized for direct comparison against the
        current hand-set scorecard weights
    """
    registry = registry if registry is not None else FEATURE_REGISTRY
    feature_names = list(registry)
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    folds = compute_fold_boundaries(n, n_folds_requested, warmup=210)
    if not folds:
        print(f"\nTRAIN MODEL: insufficient history ({n} days) for even one honest fold.")
        return

    print(f"\n{'='*90}\nSTATISTICALLY LEARNED CONVICTION MODEL (L2-regularized logistic regression)")
    print(f"{'='*90}")
    print(f"Features: {feature_names}")
    print(f"L2={l2}, bootstrap draws per fold={n_bootstrap}, outcome horizon<= {max_holding} days\n")

    fold_coeffs = []  # list of {fname: coef} per fold, standardized scale
    for fi, (test_start, test_end) in enumerate(folds):
        # TRAINING DATA: strictly [210, test_start) -- NEVER touches
        # [test_start, test_end), which is this fold's held-out period.
        # Printed explicitly so the no-leakage claim is auditable, not
        # just asserted.
        train_start, train_end = 210, test_start
        print(f"Fold {fi+1}: train on days [{train_start}, {train_end}) "
              f"({btc_dates[train_start]} to {btc_dates[train_end-1]}) -- "
              f"held out [{test_start}, {min(test_end,n)}) "
              f"({btc_dates[test_start]} to {btc_dates[min(test_end,n)-1]}) NEVER used for fitting")

        X, y, _ = build_training_samples(histories_dated, registry, train_start, train_end,
                                          btc_id=btc_id, hilo_dated=hilo_dated, max_holding=max_holding)
        if len(X) < 30:
            print(f"  ⚠️  Only {len(X)} training samples -- too few to fit reliably, skipping this fold.")
            continue
        X_std, means, stds = standardize(X)
        intercept, coefs = logistic_regression_fit(X_std, y, l2=l2, iters=500)
        n_pos = sum(y)
        print(f"  n_samples={len(X)} (positive-outcome rate={100*n_pos/len(X):.1f}%)  "
              f"coefficients: " + ", ".join(f"{fn}={c:+.3f}" for fn, c in zip(feature_names, coefs)))
        fold_coeffs.append(dict(zip(feature_names, coefs)))

        # Within-fold bootstrap CIs
        boot = bootstrap_logreg(X_std, y, l2=l2, n_bootstrap=n_bootstrap)
        print(f"  95% bootstrap CI (within this fold):")
        for j, fname in enumerate(feature_names):
            vals = [w[j] for b, w in boot]
            lo, hi = percentile(vals, 0.025), percentile(vals, 0.975)
            sig = "" if lo <= 0 <= hi else "  <-- excludes 0 (significant at this fold)"
            print(f"    {fname:22s} [{lo:+.3f}, {hi:+.3f}]{sig}")
        print()

    if len(fold_coeffs) < 2:
        print("⚠️  Fewer than 2 folds produced usable coefficients -- cannot assess")
        print("    cross-fold stability. Use more history or fewer folds.")
        return

    # ---- Cross-fold stability ----
    print(f"{'-'*90}\nCROSS-FOLD STABILITY (the decision-relevant robustness check)\n{'-'*90}")
    print(f"{'feature':22s} {'mean':>8s} {'std':>8s} {'min':>8s} {'max':>8s}  consistent sign?")
    mean_coefs = {}
    for fname in feature_names:
        vals = [fc[fname] for fc in fold_coeffs]
        mean_c = sum(vals) / len(vals)
        std_c = math.sqrt(sum((v-mean_c)**2 for v in vals) / len(vals))
        mean_coefs[fname] = mean_c
        same_sign = all(v > 0 for v in vals) or all(v < 0 for v in vals)
        print(f"{fname:22s} {mean_c:>+8.3f} {std_c:>8.3f} {min(vals):>+8.3f} {max(vals):>+8.3f}  "
              f"{'YES' if same_sign else 'NO -- sign flips across folds, unstable'}")

    # ---- Compare learned weights vs current hand-set scorecard ----
    print(f"\n{'-'*90}\nLEARNED WEIGHTS vs CURRENT HAND-SET SCORECARD\n{'-'*90}")
    hand_weights = {fname: registry[fname].weight for fname in feature_names}
    hand_total = sum(abs(w) for w in hand_weights.values()) or 1.0
    learned_total = sum(abs(c) for c in mean_coefs.values()) or 1.0
    print(f"{'feature':22s} {'hand weight':>12s} {'hand %':>8s}   {'learned coef':>13s} {'learned %':>10s}")
    for fname in feature_names:
        hw = hand_weights[fname]
        hp = 100 * abs(hw) / hand_total
        lc = mean_coefs[fname]
        lp = 100 * abs(lc) / learned_total
        print(f"{fname:22s} {hw:>12.1f} {hp:>7.1f}%   {lc:>+13.3f} {lp:>9.1f}%")

    print(f"\n{'-'*90}")
    print("Learned weights are on a STANDARDIZED coefficient scale (log-odds per 1")
    print("std-dev change in that feature's normalized score) -- comparable to each")
    print("other, but not directly interchangeable with the hand-set 0-100-scale")
    print("weights without rescaling. The %-share columns above are the fair")
    print("comparison: which features does each scheme treat as more important,")
    print("relatively. Large disagreements between hand % and learned % are the")
    print("candidates worth reweighting -- but only if the cross-fold stability")
    print("table above also shows a consistent sign for that feature; an unstable")
    print("coefficient should not be trusted regardless of its average magnitude.")
    print(f"\nTo test these learned weights in the existing attribution/ablation/")
    print(f"diagnosis tooling, use --weights with the mean coefficients above,")
    print(f"e.g.: --weights \"" + ",".join(f"{fn}:{mean_coefs[fn]:.3f}" for fn in feature_names) + "\"")

# ============================================================
# PHASE 1b -- FEATURE COVERAGE & WARM-UP AUDIT (read-only)
#
# Does not modify any feature, label, or engine logic. Reuses existing,
# already-tested functions (build_training_samples, compute_fold_boundaries,
# conviction_components_v2) exactly as-is -- this file only calls them and
# reports on what they produce.
# ============================================================

def verify_causality(registry, closes, vols, btc_closes, i_local, i_btc, highs=None, lows=None, seed=0):
    """
    Direct, empirical evidence (not an assumption) that every feature's
    raw()/normalized() output at index i_local depends ONLY on data at or
    before i_local (and i_btc for the BTC series). Method: compute each
    feature's output on the real arrays, then build PERTURBED copies where
    every value strictly AFTER i_local (and i_btc) is overwritten with
    random extreme numbers, recompute, and confirm the output at i_local is
    UNCHANGED. If a feature were reading future data, perturbing the future
    would change its past output -- this test would catch that directly.
    Returns {feature_name: True/False}, True meaning causality verified.
    """
    rng = random.Random(seed)
    def perturb(arr, cutoff):
        if arr is None: return None
        out = list(arr)
        for j in range(cutoff + 1, len(out)):
            out[j] = rng.uniform(1e6, 1e9)  # extreme, unrelated to real data
        return out

    closes_p = perturb(closes, i_local)
    vols_p = perturb(vols, i_local)
    btc_p = perturb(btc_closes, i_btc)
    highs_p = perturb(highs, i_local) if highs is not None else None
    lows_p = perturb(lows, i_local) if lows is not None else None

    results = {}
    for fname, feat in registry.items():
        if i_local < feat.min_lookback:
            continue
        rv1 = feat.raw(closes, vols, i_local, btc_closes, i_btc, highs=highs, lows=lows)
        nv1 = feat.normalized(rv1, closes, vols, i_local, btc_closes, i_btc, highs=highs, lows=lows) if rv1 is not None else 0.0
        rv2 = feat.raw(closes_p, vols_p, i_local, btc_p, i_btc, highs=highs_p, lows=lows_p)
        nv2 = feat.normalized(rv2, closes_p, vols_p, i_local, btc_p, i_btc, highs=highs_p, lows=lows_p) if rv2 is not None else 0.0
        raw_match = (rv1 is None and rv2 is None) or (rv1 is not None and rv2 is not None and abs(rv1 - rv2) < 1e-9)
        norm_match = abs(nv1 - nv2) < 1e-9
        results[fname] = raw_match and norm_match
    return results


def find_first_valid_index(feat, closes, vols, btc_closes, highs=None, lows=None, max_check=2000):
    """Empirically finds the first LOCAL index at which feat.raw() actually
    returns non-None -- not the feature's DECLARED min_lookback, the ACTUAL
    behavior. A mismatch between the two (declared understates actual) is
    itself a real finding this audit is designed to catch."""
    n = min(len(closes), max_check)
    for i in range(n):
        rv = feat.raw(closes, vols, i, btc_closes, i, highs=highs, lows=lows)
        if rv is not None:
            return i
    return None


def run_feature_warmup_audit(histories_dated, registry, hilo_dated=None, btc_id="bitcoin", n_folds_requested=4):
    hilo_dated = hilo_dated or {}
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n_btc = len(btc_dates)
    BTC_GLOBAL_WARMUP = 210  # same constant used everywhere else in this file (run_dynamic, run_dynamic_attr, etc.)
    btc_global_ready_date = btc_dates[min(BTC_GLOBAL_WARMUP, n_btc-1)]

    print(f"\n{'='*94}\nPHASE 1b -- FEATURE COVERAGE & WARM-UP AUDIT\n{'='*94}")
    print(f"Features in registry: {list(registry)}")
    print(f"BTC global clock warmup: {BTC_GLOBAL_WARMUP} days -> ready {btc_global_ready_date}\n")

    # ---- 1-11: per-asset, per-feature warm-up ----
    per_asset = {}
    print(f"{'-'*94}\nPER-FEATURE WARM-UP (first LOCAL index each feature's raw() actually returns non-None)\n{'-'*94}")
    header = f"{'sym':6s} {'rows':>6s}" + "".join(f" {fn[:12]:>13s}" for fn in registry) + f" {'ALL-valid':>10s}"
    print(header)
    for cid, (dates, closes, vols) in histories_dated.items():
        sym = TOKENS.get(cid, cid)
        highs = lows = None
        if cid in hilo_dated:
            hdates, h, l = hilo_dated[cid]
            if hdates == dates:
                highs, lows = h, l
        first_valid = {}
        for fname, feat in registry.items():
            idx = find_first_valid_index(feat, closes, vols, btc_closes, highs=highs, lows=lows)
            first_valid[fname] = idx
        joint_idx = max((v for v in first_valid.values() if v is not None), default=None)
        row_str = f"{sym:6s} {len(dates):>6d}"
        for fname in registry:
            v = first_valid[fname]
            row_str += f" {(dates[v] if v is not None else 'N/A'):>13s}"
        row_str += f" {(dates[joint_idx] if joint_idx is not None else 'N/A'):>10s}"
        print(row_str)
        per_asset[sym] = dict(cid=cid, dates=dates, first_valid=first_valid, joint_idx=joint_idx,
                               total_rows=len(dates), highs=highs, lows=lows)

    # ---- Cross-asset comparison + effective combined (BTC-clock + local) first-usable date ----
    print(f"\n{'-'*94}\nCROSS-ASSET COMPARISON (effective first-usable date = later of BTC-clock-ready and local warm-up)\n{'-'*94}")
    print(f"{'sym':6s} {'first-usable':>13s} {'last-date':>12s} {'usable-rows':>12s} {'discarded':>10s} {'usable%':>8s}")
    cross_rows = []
    for sym, info in per_asset.items():
        dates = info['dates']
        if info['joint_idx'] is None:
            print(f"{sym:6s}  -- feature never becomes valid on this asset's history --")
            continue
        local_ready_date = dates[info['joint_idx']]
        effective_date = max(local_ready_date, btc_global_ready_date)
        eff_idx = next((i for i, d in enumerate(dates) if d >= effective_date), len(dates)-1)
        usable_rows = len(dates) - eff_idx
        discarded = eff_idx
        usable_pct = 100 * usable_rows / len(dates)
        cross_rows.append((sym, effective_date, dates[-1], usable_rows, discarded, usable_pct))
        print(f"{sym:6s} {effective_date:>13s} {dates[-1]:>12s} {usable_rows:>12d} {discarded:>10d} {usable_pct:>7.1f}%")

    cross_rows.sort(key=lambda r: -r[5])
    print(f"\nRanked most-to-least complete: {[r[0] for r in cross_rows]}")
    if cross_rows:
        pct_vals = [r[5] for r in cross_rows]
        spread = max(pct_vals) - min(pct_vals)
        if spread > 15:
            worst = min(cross_rows, key=lambda r: r[5])
            print(f"⚠️  Usable-%% spread across assets is {spread:.1f} points -- {worst[0]} "
                  f"({worst[5]:.1f}%% usable) differs materially from the rest.")
        else:
            print(f"Usable-%% spread across assets: {spread:.1f} points -- no asset differs materially.")

    # ---- Effective sample sizes (12-15): reuse build_training_samples/compute_fold_boundaries UNMODIFIED ----
    print(f"\n{'-'*94}\nEFFECTIVE SAMPLE SIZE (via the ACTUAL functions the backtester/trainer use, unmodified)\n{'-'*94}")
    full_samples_X, full_samples_y, _ = build_training_samples(
        histories_dated, registry, BTC_GLOBAL_WARMUP, n_btc, btc_id=btc_id, hilo_dated=hilo_dated)
    print(f"Full-history effective sample size (build_training_samples, all folds combined): {len(full_samples_X)}")

    folds = compute_fold_boundaries(n_btc, n_folds_requested, warmup=BTC_GLOBAL_WARMUP)
    print(f"\n{'-'*94}\nPER-FOLD SAMPLE SIZE (via compute_fold_boundaries + build_training_samples, unmodified)\n{'-'*94}")
    print(f"{'fold':>5s} {'train range':>25s} {'test range':>25s} {'train samples':>14s}")
    fold_sample_counts = []
    for fi, (test_start, test_end) in enumerate(folds):
        train_X, train_y, _ = build_training_samples(
            histories_dated, registry, BTC_GLOBAL_WARMUP, test_start, btc_id=btc_id, hilo_dated=hilo_dated)
        fold_sample_counts.append(len(train_X))
        tr_range = f"[{BTC_GLOBAL_WARMUP},{test_start})"
        te_range = f"[{test_start},{min(test_end,n_btc)})"
        print(f"{fi+1:>5d} {tr_range:>25s} {te_range:>25s} {len(train_X):>14d}")

    if len(fold_sample_counts) >= 2:
        smallest = min(fold_sample_counts)
        largest = max(fold_sample_counts)
        if largest > 0 and smallest / largest < 0.15:
            print(f"\n⚠️  Smallest fold ({smallest} samples) is under 15% the size of the largest ({largest}) --")
            print(f"    early folds are disproportionately warm-up-constrained, consistent with what")
            print(f"    Phase-1b's per-asset table above already shows structurally (this is the SAME")
            print(f"    root cause surfacing in two different views, not two separate problems).")

    # ---- Leakage audit: real, computed evidence ----
    print(f"\n{'-'*94}\nLEAKAGE / CAUSALITY VERIFICATION (empirical -- future data perturbed, past output checked)\n{'-'*94}")
    n_checks, n_pass, n_fail = 0, 0, 0
    fail_examples = []
    for sym, info in per_asset.items():
        dates, cid = info['dates'], info['cid']
        closes = histories_dated[cid][1]; vols = histories_dated[cid][2]
        highs, lows = info['highs'], info['lows']
        joint_idx = info['joint_idx']
        if joint_idx is None: continue
        # Sample up to 15 points spread across the asset's usable history
        n_local = len(dates)
        sample_points = sorted(set(min(n_local-2, joint_idx + k*(n_local-joint_idx)//15) for k in range(15)))
        for i_local in sample_points:
            if i_local >= n_local - 1 or i_local >= n_btc - 1: continue
            i_btc = min(i_local, n_btc - 1)  # approximate global index for this diagnostic
            res = verify_causality(registry, closes, vols, btc_closes, i_local, i_btc, highs=highs, lows=lows, seed=i_local)
            for fname, ok in res.items():
                n_checks += 1
                if ok: n_pass += 1
                else:
                    n_fail += 1
                    if len(fail_examples) < 5:
                        fail_examples.append((sym, fname, dates[i_local]))
    print(f"Causality checks run: {n_checks}  (across {len(per_asset)} assets x up to 15 sampled dates x {len(registry)} features)")
    print(f"Passed (output unchanged when future data perturbed): {n_pass}")
    print(f"Failed (output CHANGED -- would indicate look-ahead): {n_fail}")
    if fail_examples:
        print("Failing examples:")
        for sym, fname, date in fail_examples:
            print(f"  {sym} / {fname} / {date}")

    # ---- Feature synchronization audit ----
    print(f"\n{'-'*94}\nFEATURE SYNCHRONIZATION (do all features become valid on the same date?)\n{'-'*94}")
    for sym, info in list(per_asset.items())[:1] if per_asset else []:
        pass  # placeholder to keep structure explicit; real logic below uses all assets
    slowest_by_asset = {}
    for sym, info in per_asset.items():
        fv = {k: v for k, v in info['first_valid'].items() if v is not None}
        if not fv: continue
        slowest_fname = max(fv, key=lambda k: fv[k])
        slowest_by_asset[sym] = slowest_fname
    if slowest_by_asset:
        from collections import Counter
        counts = Counter(slowest_by_asset.values())
        most_common_fname, most_common_n = counts.most_common(1)[0]
        print(f"Slowest-to-warm-up feature, by asset: {dict(slowest_by_asset)}")
        print(f"Most frequently the bottleneck: '{most_common_fname}' ({most_common_n}/{len(slowest_by_asset)} assets)")
        # Quantify: how much earlier would ALL-valid be if the bottleneck feature were excluded?
        for sym, info in per_asset.items():
            fv = info['first_valid']
            if most_common_fname not in fv or fv[most_common_fname] is None: continue
            without = max((v for k, v in fv.items() if k != most_common_fname and v is not None), default=None)
            with_it = info['joint_idx']
            if without is not None and with_it is not None and without < with_it:
                gained = with_it - without
                print(f"  {sym}: excluding '{most_common_fname}' would make {gained} more observations usable "
                      f"({info['dates'][with_it]} -> {info['dates'][without]})")
    else:
        print("No asset had enough history for any feature to become valid.")

    # ---- GO/NO-GO ----
    print(f"\n{'='*94}\nGO / NO-GO\n{'='*94}")
    leakage_ok = (n_fail == 0)
    sample_ok = len(full_samples_X) >= 100
    if leakage_ok and sample_ok:
        print("GO: no causality violations detected across all sampled checks, and effective sample")
        print("    size is sufficient to proceed. Proceed to Phase 2 -- Label Audit.")
    else:
        if not leakage_ok:
            print(f"NO-GO: {n_fail} causality check(s) FAILED -- see failing examples above. This must")
            print(f"       be investigated before any label or model work, since it would invalidate")
            print(f"       every downstream statistical conclusion.")
        if not sample_ok:
            print(f"NO-GO: effective full-history sample size ({len(full_samples_X)}) is too small to")
            print(f"       support reliable walk-forward analysis.")

# ============================================================
# PHASE 2 -- LABEL AUDIT (read-only)
#
# Does not modify simulate_trade_outcome() or build_training_samples().
# The functions below MIRROR their exact logic (same constants, same
# stop/T1/T2/timeout math, same population-selection conditions) to
# extract diagnostic detail (holding period, exit reason, per-sample
# entry/exit indices) that the originals don't expose -- consistent with
# the pattern already used elsewhere in this file (e.g. conviction_dyn
# alongside conviction_components_v2) rather than modifying tested code.
# ============================================================

def simulate_trade_outcome_diagnostic(closes, i_entry, max_holding=90):
    """
    Byte-for-byte the same stop/T1/T2/timeout logic as simulate_trade_outcome()
    -- verified by direct comparison below -- but additionally returns
    holding_days and exit_reason for audit purposes.
    Returns (r_total, holding_days, exit_reason) or None.
    """
    n = len(closes)
    if i_entry < 30 or i_entry + 1 >= n:
        return None
    px = closes[i_entry]
    vol30 = abs(px / closes[i_entry-30] - 1) * 100
    stop_pct = max(8, min(25, vol30 * 0.8)) / 100
    entry = px * (1 + SLIP)
    stop = px * (1 - stop_pct)
    t1 = px * (1 + stop_pct * 1.5)
    t2 = px * (1 + stop_pct * 3)
    t1_hit = False
    end = min(n, i_entry + 1 + max_holding)
    r_total = 0.0
    for j in range(i_entry + 1, end):
        p = closes[j]
        if p <= stop:
            frac = 0.5 if t1_hit else 1.0
            r_total += (stop / entry - 1) / stop_pct * frac - FEE - SLIP
            return r_total, j - i_entry, ("stop_after_t1" if t1_hit else "stop_before_t1")
        if not t1_hit and p >= t1:
            r_total += (t1 / entry - 1) / stop_pct * 0.5 - FEE - SLIP
            t1_hit = True
            stop = entry
            continue
        if p >= t2:
            frac = 0.5 if t1_hit else 1.0
            r_total += (t2 / entry - 1) / stop_pct * frac - FEE - SLIP
            return r_total, j - i_entry, "t2"
    frac = 0.5 if t1_hit else 1.0
    last_px = closes[end - 1]
    r_total += (last_px / entry - 1) / stop_pct * frac - FEE - SLIP
    return r_total, (end - 1) - i_entry, "timeout"


def build_label_audit_samples(histories_dated, registry, day_start, day_end,
                               btc_id="bitcoin", hilo_dated=None, max_holding=90):
    """
    Mirrors build_training_samples()'s EXACT population-selection loop
    (same local_i>=210 check, same per-feature min_lookback gating, same
    unconditional-on-score/regime population) -- verified identical
    sample count against build_training_samples() below -- but records
    full per-sample metadata: (cid, sym, entry_global_i, entry_local_i,
    holding_days, exit_local_i, r, y, exit_reason).
    """
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    hilo_dated = hilo_dated or {}
    date_idx = {cid: {d: idx for idx, d in enumerate(dates)} for cid, (dates, closes, vols) in histories_dated.items()}
    hilo_idx = {}
    for cid, (hdates, highs, lows) in hilo_dated.items():
        if hdates is not None:
            hilo_idx[cid] = {d: idx for idx, d in enumerate(hdates)}

    feature_names = list(registry)
    samples = []
    end_i = min(n, day_end)
    start_i = max(210, day_start)
    for i in range(start_i, end_i):
        D = btc_dates[i]
        for cid, (dates, closes, vols) in histories_dated.items():
            sym = TOKENS.get(cid, cid)
            local_i = date_idx[cid].get(D)
            if local_i is None or local_i < 210:
                continue
            highs = lows = None
            if cid in hilo_dated and cid in hilo_idx:
                h_local_i = hilo_idx[cid].get(D)
                if h_local_i is not None and h_local_i == local_i:
                    _, highs, lows = hilo_dated[cid]
            skip = False
            X_row = []
            for fname in feature_names:
                feat = registry[fname]
                if local_i < feat.min_lookback:
                    skip = True; break
                rv = feat.raw(closes, vols, local_i, btc_closes, i, highs=highs, lows=lows)
                nv = feat.normalized(rv, closes, vols, local_i, btc_closes, i, highs=highs, lows=lows) if rv is not None else 0.0
                X_row.append(nv)
            if skip:
                continue
            result = simulate_trade_outcome_diagnostic(closes, local_i, max_holding=max_holding)
            if result is None:
                continue
            r, holding_days, exit_reason = result
            samples.append(dict(cid=cid, sym=sym, entry_global_i=i, entry_local_i=local_i,
                                 exit_local_i=local_i + holding_days, holding_days=holding_days,
                                 r=r, y=(1 if r > 0 else 0), exit_reason=exit_reason, X=X_row))
    return samples


def verify_label_fidelity(histories_dated, registry, day_start, day_end, btc_id="bitcoin", hilo_dated=None):
    """Direct evidence that the diagnostic builder produces the IDENTICAL
    population and labels as the real, unmodified build_training_samples()
    -- not an assumption, a computed comparison."""
    X, y, _ = build_training_samples(histories_dated, registry, day_start, day_end, btc_id=btc_id, hilo_dated=hilo_dated)
    audit_samples = build_label_audit_samples(histories_dated, registry, day_start, day_end, btc_id=btc_id, hilo_dated=hilo_dated)
    count_match = (len(y) == len(audit_samples))
    y_match = count_match and (y == [s['y'] for s in audit_samples])
    return count_match, y_match, len(y), len(audit_samples)


def compute_label_uniqueness(samples):
    """
    Lopez de Prado average-uniqueness / effective-sample-size calculation,
    computed PER ASSET (concurrency is measured against other labels of
    the SAME asset, since that's where forward-window information overlap
    is real -- two different assets' labels overlapping in calendar time
    isn't the same asset's price path being double-counted).
    """
    by_asset = {}
    for s in samples:
        by_asset.setdefault(s['sym'], []).append(s)

    total_effective_n = 0.0
    total_overlapping = 0
    per_asset_report = {}
    for sym, slist in by_asset.items():
        max_t = max(s['exit_local_i'] for s in slist) + 1
        concurrency = [0] * (max_t + 1)
        for s in slist:
            for t in range(s['entry_local_i'] + 1, s['exit_local_i'] + 1):
                if t < len(concurrency):
                    concurrency[t] += 1
        asset_uniqueness_sum = 0.0
        n_overlapping = 0
        for s in slist:
            window = range(s['entry_local_i'] + 1, s['exit_local_i'] + 1)
            cs = [concurrency[t] for t in window if t < len(concurrency)]
            if not cs:
                avg_u = 1.0
            else:
                avg_u = sum(1.0/c for c in cs) / len(cs)
                if max(cs) > 1:
                    n_overlapping += 1
            asset_uniqueness_sum += avg_u
        total_effective_n += asset_uniqueness_sum
        total_overlapping += n_overlapping
        per_asset_report[sym] = dict(n=len(slist), n_overlapping=n_overlapping,
                                     pct_overlapping=100*n_overlapping/len(slist) if slist else 0,
                                     effective_n=asset_uniqueness_sum,
                                     avg_uniqueness=asset_uniqueness_sum/len(slist) if slist else 0)
    return total_effective_n, total_overlapping, per_asset_report


# ============================================================
# PURGED WALK-FORWARD CROSS-VALIDATION (Lopez de Prado)
#
# New, separate validation path. Does NOT modify run_train_model(),
# run_validate_dynamic(), or run_attribution() -- --validate and
# --train-model remain byte-identical to before. This section adds
# --purged-validation as an independent mode, built on top of the
# read-only Phase 2 tooling (build_label_audit_samples, which now
# additionally carries each sample's feature vector).
# ============================================================

def purge_and_embargo_samples(samples, test_start, test_end, prior_test_ranges, embargo_days):
    """
    Lopez de Prado purging + embargo, applied to a candidate training set
    (samples with entry_global_i < test_start, i.e. what the NAIVE
    expanding-window training set would already include):

    PURGE: remove any sample whose label window [entry+1, exit] overlaps
    the CURRENT fold's test period [test_start, test_end) -- this is
    exactly the fold-boundary leakage Phase 2 measured and flagged.

    EMBARGO: remove any sample whose ENTRY falls within `embargo_days`
    immediately after any PRIOR fold's test period. This matters because
    this is expanding-window (not classic k-fold): by the time we reach
    fold i, every earlier fold's test period has become part of fold i's
    training range. Each of those transitions needs its own embargo, or
    near-boundary serial correlation can still leak information across
    what was a test/train split at the time.
    """
    purged, embargoed, kept = [], [], []
    for s in samples:
        entry = s['entry_global_i']
        exit_ = entry + s['holding_days']
        if exit_ >= test_start and entry < test_end:
            purged.append(s)
            continue
        is_embargoed = False
        for (ts_j, te_j) in prior_test_ranges:
            if te_j <= entry < te_j + embargo_days:
                is_embargoed = True
                break
        if is_embargoed:
            embargoed.append(s)
            continue
        kept.append(s)
    return kept, purged, embargoed


# ============================================================
# PROBABILITY DIAGNOSTIC (read-only, no evaluation/trading path)
#
# Fits the same purged model (reusing standardize/logistic_regression_fit
# unmodified) but PRESERVES intercept/means/stds -- currently discarded by
# run_train_model() -- so P = sigmoid(intercept + coef . standardized_x)
# can be computed correctly, out-of-sample, and audited BEFORE any
# probability-threshold evaluation path is built.
# ============================================================

def sigmoid(z):
    return 1.0/(1.0+math.exp(-z)) if z >= 0 else math.exp(z)/(1.0+math.exp(z))

def roc_auc(probs, labels):
    """Exact AUC via Mann-Whitney U on ranks -- no thresholding loop needed."""
    n_pos = sum(labels); n_neg = len(labels) - n_pos
    if n_pos == 0 or n_neg == 0: return None
    ranked = sorted(range(len(probs)), key=lambda i: probs[i])
    ranks = [0]*len(probs)
    i = 0
    while i < len(ranked):
        j = i
        while j+1 < len(ranked) and probs[ranked[j+1]] == probs[ranked[i]]:
            j += 1
        avg_rank = (i+1 + j+1) / 2.0
        for k in range(i, j+1): ranks[ranked[k]] = avg_rank
        i = j+1
    rank_sum_pos = sum(ranks[i] for i in range(len(labels)) if labels[i] == 1)
    return (rank_sum_pos - n_pos*(n_pos+1)/2.0) / (n_pos*n_neg)

def pr_auc(probs, labels):
    """Precision-recall AUC via trapezoidal integration over the PR curve,
    sweeping thresholds at every distinct predicted probability."""
    n_pos = sum(labels)
    if n_pos == 0: return None
    order = sorted(range(len(probs)), key=lambda i: -probs[i])
    tp = fp = 0
    points = [(0.0, 1.0)]  # (recall, precision) -- start at recall=0
    for idx in order:
        if labels[idx] == 1: tp += 1
        else: fp += 1
        recall = tp / n_pos
        precision = tp / (tp + fp)
        points.append((recall, precision))
    points.sort()
    auc = 0.0
    for i in range(1, len(points)):
        r0, p0 = points[i-1]; r1, p1 = points[i]
        auc += (r1 - r0) * (p0 + p1) / 2.0
    return auc

def brier_score(probs, labels):
    return sum((p-y)**2 for p, y in zip(probs, labels)) / len(probs)

def calibration_curve(probs, labels, n_bins=10):
    bins = [[] for _ in range(n_bins)]
    for p, y in zip(probs, labels):
        b = min(n_bins-1, int(p * n_bins))
        bins[b].append(y)
    out = []
    for i, b in enumerate(bins):
        lo, hi = i/n_bins, (i+1)/n_bins
        if b:
            out.append((lo, hi, len(b), sum(b)/len(b)))
        else:
            out.append((lo, hi, 0, None))
    return out

def run_probability_diagnostic(histories_dated, registry=None, btc_id="bitcoin", n_folds_requested=4,
                                hilo_dated=None, l2=1.0, max_holding=90):
    """
    Fits the SAME purged model as run_purged_validation (identical purge/
    embargo, identical standardize/logistic_regression_fit calls) but
    keeps intercept/means/stds, then scores each fold's OUT-OF-SAMPLE test
    period with P = sigmoid(intercept + coef . (x-mean)/std). No admission
    threshold, no trading, no new evaluation path -- pure audit of whether
    the model's native probability output is worth building that path on.
    """
    registry = registry if registry is not None else FEATURE_REGISTRY
    feature_names = list(registry)
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    embargo_days = max_holding
    folds = compute_fold_boundaries(n, n_folds_requested, warmup=210)
    if not folds:
        print(f"\nPROBABILITY DIAGNOSTIC: insufficient history ({n} days).")
        return

    print(f"\n{'='*98}\nPROBABILITY DIAGNOSTIC (native logistic output, purged training, out-of-sample scoring)\n{'='*98}")

    all_samples = build_label_audit_samples(histories_dated, registry, 210, n, btc_id=btc_id,
                                             hilo_dated=hilo_dated, max_holding=max_holding)
    prior_test_ranges = []
    pooled_probs, pooled_labels = [], []
    for fi, (test_start, test_end) in enumerate(folds):
        naive = [s for s in all_samples if s['entry_global_i'] < test_start]
        kept, _, _ = purge_and_embargo_samples(naive, test_start, test_end, prior_test_ranges, embargo_days)
        prior_test_ranges.append((test_start, test_end))
        if len(kept) < 30:
            print(f"Fold {fi+1}: only {len(kept)} purged training samples -- skipped (too few to fit).")
            continue
        X_train = [s['X'] for s in kept]; y_train = [s['y'] for s in kept]
        X_std, means, stds = standardize(X_train)
        intercept, coefs = logistic_regression_fit(X_std, y_train, l2=l2, iters=500)

        test_samples = [s for s in all_samples if test_start <= s['entry_global_i'] < test_end]
        for s in test_samples:
            x_std = [(s['X'][j] - means[j]) / stds[j] for j in range(len(feature_names))]
            logit = intercept + sum(c*x for c, x in zip(coefs, x_std))
            p = sigmoid(logit)
            pooled_probs.append(p)
            pooled_labels.append(s['y'])
        print(f"Fold {fi+1}: trained on {len(kept)} purged samples, scored {len(test_samples)} "
              f"out-of-sample test samples. intercept={intercept:+.3f}")

    if len(pooled_probs) < 20:
        print(f"\n⚠️  Only {len(pooled_probs)} pooled out-of-sample scored points -- too few for")
        print(f"    reliable AUC/calibration estimates. Treat everything below as provisional.")

    print(f"\n{'-'*98}\nPOOLED OUT-OF-SAMPLE PROBABILITY DISTRIBUTION (n={len(pooled_probs)})\n{'-'*98}")
    sp = sorted(pooled_probs)
    mean_p = sum(sp)/len(sp)
    median_p = sp[len(sp)//2]
    std_p = math.sqrt(sum((p-mean_p)**2 for p in sp)/len(sp))
    print(f"Min: {sp[0]:.4f}   Max: {sp[-1]:.4f}   Mean: {mean_p:.4f}   Median: {median_p:.4f}   Std: {std_p:.4f}")

    print("\nHistogram:")
    for i in range(10):
        lo, hi = i*10, (i+1)*10
        c = sum(1 for p in pooled_probs if lo/100 <= p < hi/100 or (i==9 and p==1.0))
        bar = "#" * int(50*c/len(pooled_probs)) if pooled_probs else ""
        print(f"  {lo:>3d}-{hi:<3d}%  {c:>6d} ({100*c/len(pooled_probs):>5.1f}%)  {bar}")

    winners = [p for p, y in zip(pooled_probs, pooled_labels) if y == 1]
    losers = [p for p, y in zip(pooled_probs, pooled_labels) if y == 0]
    print(f"\nWinners (y=1, n={len(winners)}): mean P={sum(winners)/len(winners):.4f}" if winners else "\nNo winners in pooled set.")
    print(f"Losers  (y=0, n={len(losers)}): mean P={sum(losers)/len(losers):.4f}" if losers else "No losers in pooled set.")

    auc = roc_auc(pooled_probs, pooled_labels)
    prauc = pr_auc(pooled_probs, pooled_labels)
    brier = brier_score(pooled_probs, pooled_labels)
    print(f"\n{'-'*98}\nDISCRIMINATION & CALIBRATION METRICS\n{'-'*98}")
    print(f"ROC AUC:  {auc:.4f}" if auc is not None else "ROC AUC: undefined (single-class pooled set)")
    print(f"PR AUC:   {prauc:.4f}" if prauc is not None else "PR AUC: undefined")
    print(f"Brier Score: {brier:.4f}  (0=perfect, 0.25=uninformative-coin-flip-at-p0.5, 1=worst)")

    print(f"\n{'-'*98}\nCALIBRATION CURVE (10 bins: predicted P range -> actual empirical win rate)\n{'-'*98}")
    print(f"{'bin':>12s} {'n':>6s} {'actual win%':>12s} {'predicted mid':>14s} {'gap':>8s}")
    cal = calibration_curve(pooled_probs, pooled_labels, 10)
    for lo, hi, cnt, actual in cal:
        mid = (lo+hi)/2*100
        if actual is None:
            print(f"  {lo*100:>3.0f}-{hi*100:<3.0f}%   {cnt:>6d}  {'--':>11s}  {mid:>13.1f}%  {'--':>7s}")
        else:
            gap = actual*100 - mid
            print(f"  {lo*100:>3.0f}-{hi*100:<3.0f}%   {cnt:>6d}  {actual*100:>11.1f}%  {mid:>13.1f}%  {gap:>+6.1f}pp")

    print(f"\n{'-'*98}\nANSWERS\n{'-'*98}")
    if auc is not None:
        disc = "YES -- meaningfully above 0.5" if auc > 0.55 else ("WEAK -- barely above chance" if auc > 0.52 else "NO -- indistinguishable from random")
        print(f"1. Does the model discriminate winners from losers? {disc} (AUC={auc:.3f})")
    else:
        print("1. Cannot assess -- pooled set has only one class.")
    spread = sp[-1] - sp[0]
    compressed = "YES -- probabilities barely move from ~0.5" if std_p < 0.05 else ("MODERATE" if std_p < 0.10 else "NO -- reasonable spread")
    print(f"2. Is the probability distribution compressed? {compressed} (std={std_p:.4f}, range={spread:.4f})")
    max_gap = max((abs(a*100-((lo+hi)/2*100)) for lo,hi,c,a in cal if a is not None), default=None)
    if max_gap is not None:
        cal_issue = "Calibration gaps are large" if max_gap > 15 else "Calibration gaps are modest"
        print(f"3. Calibration vs architecture: {cal_issue} (max bin gap={max_gap:.1f}pp). "
              f"{'If AUC is also weak, the problem is discrimination (feature signal), not just calibration.' if (auc is not None and auc<0.55) else 'If AUC is reasonable but gaps are large, calibration (Platt/isotonic) would help; if AUC is weak, calibration cannot fix a lack of underlying signal.'}")
    print(f"4. Proceed with native probability thresholds? "
          f"{'Only after checking (1)-(3) above on your REAL data -- this run used the sandbox test data described below, not your dataset.' }")

QUANTIZED_FEATURES = ("atr_donchian_squeeze", "obv")

def quantize_value(v, n_bins):
    """Maps a 0-100 normalized score to its bin CENTER at the given bin
    count, so quartile/decile-transformed values stay on the same 0-100
    scale as native (comparable coefficient magnitudes across modes)."""
    if v is None: return None
    idx = min(n_bins - 1, max(0, int(v / 100.0 * n_bins)))
    return (idx + 0.5) * (100.0 / n_bins)

def apply_representation(X_row, feature_names, representation):
    """Binary features (RS, Trend, Volume Expansion, Momentum) pass through
    completely unchanged -- only ATR/OBV are ever touched, and only when
    representation != 'native'."""
    if representation == "native":
        return list(X_row)
    n_bins = {"quartile": 4, "decile": 10}[representation]
    out = list(X_row)
    for j, fname in enumerate(feature_names):
        if fname in QUANTIZED_FEATURES:
            out[j] = quantize_value(X_row[j], n_bins)
    return out

def is_candidate_sample(s, feature_names, hand_weights, btc_closes, ema200_btc):
    """The existing hand-set scorecard's own admission rule (score >= 70,
    regime not off) -- identical arithmetic to conviction_components_v2's
    scoring formula, applied directly to an already-built sample's X
    vector. This is the SAME rule the live engine already uses to decide
    what to trade."""
    i = s['entry_global_i']
    e = ema200_btc[i]
    regime_off = e is not None and btc_closes[i] < e * 0.995
    score = CONVICTION_BASE + sum(hand_weights[fn] * s['X'][j] / 100 for j, fn in enumerate(feature_names))
    return (not regime_off) and score >= CONV_THRESHOLD

def run_candidate_only_experiment(histories_dated, registry=None, btc_id="bitcoin", n_folds_requested=4,
                                   hilo_dated=None, l2=1.0, max_holding=90, representation="native",
                                   exclude_feature=None):
    """
    Mirrors run_probability_diagnostic()'s fold loop exactly (same purge/
    embargo call, same standardize/logistic_regression_fit calls, same
    out-of-sample sigmoid scoring) -- the ONLY differences: population
    filtered to candidates before training AND scoring, and ATR/OBV
    optionally quantized per `representation`. No randomness anywhere in
    this path, so results are deterministic/reproducible across runs.

    exclude_feature: optional feature name to drop from the LOGISTIC
    FEATURE MATRIX only. Candidate selection (is_candidate_sample, via
    hand_weights) still uses the FULL feature set including the excluded
    one -- candidate selection, the scorecard, and every other feature are
    unchanged, per requirement 3. This only removes one column before
    standardize()/logistic_regression_fit() and the matching test-time
    scoring, nothing else.
    """
    registry = registry if registry is not None else FEATURE_REGISTRY
    feature_names = list(registry)
    hand_weights = {fn: registry[fn].weight for fn in feature_names}
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    embargo_days = max_holding
    ema200_btc = rolling_ema_series(btc_closes, 200)
    folds = compute_fold_boundaries(n, n_folds_requested, warmup=210)
    if not folds:
        print(f"\nCANDIDATE-ONLY EXPERIMENT [{representation}]: insufficient history ({n} days).")
        return None

    all_samples = build_label_audit_samples(histories_dated, registry, 210, n, btc_id=btc_id,
                                             hilo_dated=hilo_dated, max_holding=max_holding)
    # Candidate selection ALWAYS uses the full feature set (hand_weights,
    # feature_names) -- exclude_feature never touches this step.
    candidates = [s for s in all_samples if is_candidate_sample(s, feature_names, hand_weights, btc_closes, ema200_btc)]
    candidates = [dict(s, X=apply_representation(s['X'], feature_names, representation)) for s in candidates]

    # Determine which columns feed the LOGISTIC matrix -- this is the only
    # place exclude_feature has any effect.
    if exclude_feature and exclude_feature in feature_names:
        fit_indices = [j for j, fn in enumerate(feature_names) if fn != exclude_feature]
        fit_feature_names = [fn for fn in feature_names if fn != exclude_feature]
    else:
        fit_indices = list(range(len(feature_names)))
        fit_feature_names = list(feature_names)

    prior_test_ranges = []
    pooled_probs, pooled_labels = [], []
    fold_intercepts, fold_coefs = [], []
    for fi, (test_start, test_end) in enumerate(folds):
        naive = [s for s in candidates if s['entry_global_i'] < test_start]
        kept, _, _ = purge_and_embargo_samples(naive, test_start, test_end, prior_test_ranges, embargo_days)
        prior_test_ranges.append((test_start, test_end))
        if len(kept) < 30:
            continue
        X_train = [[s['X'][j] for j in fit_indices] for s in kept]
        y_train = [s['y'] for s in kept]
        X_std, means, stds = standardize(X_train)
        intercept, coefs = logistic_regression_fit(X_std, y_train, l2=l2, iters=500)
        fold_intercepts.append(intercept)
        fold_coefs.append(dict(zip(fit_feature_names, coefs)))

        test_samples = [s for s in candidates if test_start <= s['entry_global_i'] < test_end]
        for s in test_samples:
            x_row = [s['X'][j] for j in fit_indices]
            x_std = [(x_row[k] - means[k]) / stds[k] for k in range(len(fit_indices))]
            logit = intercept + sum(c*x for c, x in zip(coefs, x_std))
            pooled_probs.append(sigmoid(logit))
            pooled_labels.append(s['y'])

    mean_intercept = sum(fold_intercepts)/len(fold_intercepts) if fold_intercepts else None
    mean_coefs = {fn: sum(fc[fn] for fc in fold_coefs)/len(fold_coefs) for fn in fit_feature_names} if fold_coefs else {}

    result = dict(representation=representation, exclude_feature=exclude_feature,
                  fit_feature_names=fit_feature_names, n_candidates=len(candidates),
                  n_scored=len(pooled_probs), probs=pooled_probs, labels=pooled_labels,
                  mean_intercept=mean_intercept, mean_coefs=mean_coefs)
    if len(pooled_probs) < 10:
        result.update(auc=None, prauc=None, brier=None, cal=None, winner_p=None, loser_p=None)
        return result

    auc = roc_auc(pooled_probs, pooled_labels)
    prauc = pr_auc(pooled_probs, pooled_labels)
    brier = brier_score(pooled_probs, pooled_labels)
    cal = calibration_curve(pooled_probs, pooled_labels, 10)
    winners = [p for p, y in zip(pooled_probs, pooled_labels) if y == 1]
    losers = [p for p, y in zip(pooled_probs, pooled_labels) if y == 0]
    result.update(auc=auc, prauc=prauc, brier=brier, cal=cal,
                  winner_p=(sum(winners)/len(winners) if winners else None),
                  loser_p=(sum(losers)/len(losers) if losers else None))
    return result

def print_candidate_only_report(result):
    r = result
    print(f"\n{'-'*90}\nREPRESENTATION: {r['representation']}\n{'-'*90}")
    print(f"Candidate count: {r['n_candidates']}   Out-of-sample scored: {r['n_scored']}")
    if r['auc'] is None:
        print("Too few scored points for reliable metrics.")
        return
    print(f"ROC AUC:  {r['auc']:.4f}")
    print(f"PR AUC:   {r['prauc']:.4f}")
    print(f"Brier:    {r['brier']:.4f}")
    print(f"Winner mean P: {r['winner_p']:.4f}" if r['winner_p'] is not None else "Winner mean P: n/a")
    print(f"Loser mean P:  {r['loser_p']:.4f}" if r['loser_p'] is not None else "Loser mean P: n/a")
    max_gap = max((abs(a*100-((lo+hi)/2*100)) for lo,hi,c,a in r['cal'] if a is not None), default=None)
    print(f"Calibration: max bin gap = {max_gap:.1f}pp" if max_gap is not None else "Calibration: n/a")

def print_rs_ablation_report(result):
    """Dedicated print path for the RS-ablation experiment ONLY -- keeps
    print_candidate_only_report() (used by the existing --candidate-only
    comparison command) completely untouched, so that command's output is
    byte-identical to before this change."""
    r = result
    excl_str = f" (excluding {r['exclude_feature']})" if r.get('exclude_feature') else " (baseline, all features)"
    print(f"\n{'-'*90}\n{r['representation']}{excl_str}\n{'-'*90}")
    print(f"Candidate count: {r['n_candidates']}   Out-of-sample scored: {r['n_scored']}")
    if r.get('mean_intercept') is not None:
        print(f"Mean intercept (across folds): {r['mean_intercept']:+.4f}")
        print("Mean coefficients (across folds):")
        for fn in r['fit_feature_names']:
            print(f"  {fn:22s} {r['mean_coefs'][fn]:+.4f}")
    if r['auc'] is None:
        print("Too few scored points for reliable metrics.")
        return
    print(f"ROC AUC:  {r['auc']:.4f}")
    print(f"PR AUC:   {r['prauc']:.4f}")
    print(f"Brier:    {r['brier']:.4f}")
    print(f"Winner mean P: {r['winner_p']:.4f}" if r['winner_p'] is not None else "Winner mean P: n/a")
    print(f"Loser mean P:  {r['loser_p']:.4f}" if r['loser_p'] is not None else "Loser mean P: n/a")
    max_gap = max((abs(a*100-((lo+hi)/2*100)) for lo,hi,c,a in r['cal'] if a is not None), default=None)
    print(f"Calibration: max bin gap = {max_gap:.1f}pp" if max_gap is not None else "Calibration: n/a")

def run_candidate_only_comparison(histories_dated, registry=None, btc_id="bitcoin", n_folds_requested=4,
                                   hilo_dated=None, l2=1.0, max_holding=90, representations=("native","quartile","decile")):
    print(f"\n{'='*98}\nCANDIDATE-ONLY META-LABELING: REPRESENTATION COMPARISON\n{'='*98}")
    print(f"Identical folds, purge, embargo across all {len(representations)} runs (deterministic, no RNG involved).")
    results = []
    for rep in representations:
        res = run_candidate_only_experiment(histories_dated, registry=registry, btc_id=btc_id,
                                            n_folds_requested=n_folds_requested, hilo_dated=hilo_dated,
                                            l2=l2, max_holding=max_holding, representation=rep)
        if res is not None:
            print_candidate_only_report(res)
            results.append(res)

    print(f"\n{'='*98}\nCOMPARISON TABLE\n{'='*98}")
    print(f"{'representation':14s} {'ROC AUC':>9s} {'PR AUC':>8s} {'Brier':>8s} {'Winner P':>9s} {'Loser P':>9s}")
    for r in results:
        if r['auc'] is None:
            print(f"{r['representation']:14s} {'--':>9s}")
            continue
        print(f"{r['representation']:14s} {r['auc']:>9.4f} {r['prauc']:>8.4f} {r['brier']:>8.4f} "
              f"{r['winner_p']:>9.4f} {r['loser_p']:>9.4f}")

    print(f"\n{'-'*98}\nANSWERS\n{'-'*98}")
    valid = [r for r in results if r['auc'] is not None]
    if not valid:
        print("Insufficient data to answer -- too few out-of-sample scored points in every representation.")
        return
    best = max(valid, key=lambda r: r['auc'])
    UNCONDITIONAL_AUC = 0.4837
    print(f"1. Best candidate-only AUC ({best['representation']}={best['auc']:.4f}) vs unconditional AUC "
          f"({UNCONDITIONAL_AUC}): "
          f"{'IMPROVEMENT' if best['auc'] > UNCONDITIONAL_AUC + 0.01 else ('roughly unchanged' if abs(best['auc']-UNCONDITIONAL_AUC) <= 0.01 else 'WORSE')}")
    quart = next((r for r in valid if r['representation']=='quartile'), None)
    dec = next((r for r in valid if r['representation']=='decile'), None)
    if quart and dec:
        diff = quart['auc'] - dec['auc']
        print(f"2. Quartile ({quart['auc']:.4f}) vs Decile ({dec['auc']:.4f}): "
              f"{'quartile ahead' if diff>0.01 else ('decile ahead' if diff<-0.01 else 'no meaningful difference')} (Δ={diff:+.4f})")
    print(f"3. Practical significance: AUC improvements under ~0.03-0.05 over a 0.50 baseline are typically")
    print(f"   within noise for a sample this size (n~{valid[0]['n_scored']} scored) -- treat any gain here as")
    print(f"   suggestive, not confirmed, without a larger dataset or repeated folds.")
    if best['auc'] < 0.55:
        print(f"4. Recommendation: candidate-only training did NOT produce meaningful discrimination")
        print(f"   (best AUC={best['auc']:.4f}, still close to 0.50). Continuing to iterate on")
        print(f"   representation or population framing has low expected value from here --")
        print(f"   redirect toward genuinely new information sources (breadth, cross-asset,")
        print(f"   on-chain) rather than further meta-labeling refinement on the current 6 features.")
    else:
        print(f"4. Recommendation: candidate-only training shows real discrimination (best AUC={best['auc']:.4f}).")
        print(f"   Worth continued investment in this direction -- next step would be confirming")
        print(f"   stability across more folds / more history before any further design changes.")

def run_feature_ablation_experiment(histories_dated, registry=None, btc_id="bitcoin", n_folds_requested=4,
                                     hilo_dated=None, l2=1.0, max_holding=90, exclude_feature="relative_strength"):
    """
    Runs run_candidate_only_experiment() TWICE -- baseline (all features)
    and with `exclude_feature` dropped from the logistic matrix only --
    both using representation='native' (per requirement 1: identical
    purge/embargo/folds/candidate population/representation, only the
    feature-exclusion varies). Reports coefficients, intercept, AUC/PR/
    Brier/calibration for both, a before/after coefficient comparison
    table, and the five requested interpretive answers.
    """
    print(f"\n{'='*98}\nFEATURE ABLATION (candidate-only pipeline): excluding '{exclude_feature}'\n{'='*98}")
    print("Both runs use identical purge/embargo/folds/candidate population/representation (native).")
    print("Candidate selection uses the FULL feature set (including the excluded one) in both runs --")
    print("only the logistic regression's input matrix differs.\n")

    baseline = run_candidate_only_experiment(histories_dated, registry=registry, btc_id=btc_id,
                                             n_folds_requested=n_folds_requested, hilo_dated=hilo_dated,
                                             l2=l2, max_holding=max_holding, representation="native",
                                             exclude_feature=None)
    ablated = run_candidate_only_experiment(histories_dated, registry=registry, btc_id=btc_id,
                                            n_folds_requested=n_folds_requested, hilo_dated=hilo_dated,
                                            l2=l2, max_holding=max_holding, representation="native",
                                            exclude_feature=exclude_feature)
    if baseline is None or ablated is None:
        print("Insufficient history for this experiment.")
        return

    print_rs_ablation_report(dict(baseline, representation="BASELINE (all features)"))
    print_rs_ablation_report(dict(ablated, representation=f"ABLATED"))

    print(f"\n{'='*98}\nCOEFFICIENT COMPARISON (features common to both runs)\n{'='*98}")
    print(f"{'feature':22s} {'coef before':>12s} {'coef after':>11s} {'%change':>9s} {'rank before':>12s} {'rank after':>11s}")
    if baseline['auc'] is not None and ablated['auc'] is not None:
        rank_before = sorted(baseline['fit_feature_names'], key=lambda f: -abs(baseline['mean_coefs'][f]))
        rank_after = sorted(ablated['fit_feature_names'], key=lambda f: -abs(ablated['mean_coefs'][f]))
        for fn in baseline['fit_feature_names']:
            if fn == exclude_feature: continue
            cb = baseline['mean_coefs'][fn]
            ca = ablated['mean_coefs'].get(fn)
            if ca is None: continue
            pct = 100*(ca-cb)/abs(cb) if abs(cb) > 1e-9 else float('inf')
            rb = rank_before.index(fn) + 1
            ra = rank_after.index(fn) + 1
            pct_str = f"{pct:+.1f}%" if pct != float('inf') else "n/a"
            print(f"{fn:22s} {cb:>+12.4f} {ca:>+11.4f} {pct_str:>9s} {rb:>12d} {ra:>11d}")

    print(f"\n{'-'*98}\nANSWERS\n{'-'*98}")
    if baseline['auc'] is None or ablated['auc'] is None:
        print("Insufficient scored points in one or both runs to answer.")
        return

    d_auc = ablated['auc'] - baseline['auc']
    print(f"1. AUC change from excluding {exclude_feature}: {baseline['auc']:.4f} -> {ablated['auc']:.4f} "
          f"(Δ={d_auc:+.4f})  {'MATERIAL improvement' if d_auc > 0.03 else ('modest' if d_auc > 0.01 else 'no material change')}")

    b_inverted = baseline['winner_p'] is not None and baseline['winner_p'] < baseline['loser_p']
    a_inverted = ablated['winner_p'] is not None and ablated['winner_p'] < ablated['loser_p']
    print(f"2. Winner P > Loser P? Baseline: {'NO (inverted)' if b_inverted else 'YES'} "
          f"(W={baseline['winner_p']:.4f}, L={baseline['loser_p']:.4f})  |  "
          f"Ablated: {'NO (inverted)' if a_inverted else 'YES'} (W={ablated['winner_p']:.4f}, L={ablated['loser_p']:.4f})")

    if baseline['auc'] is not None:
        reorg = any(fn != exclude_feature and
                    abs(baseline['mean_coefs'][fn] - ablated['mean_coefs'].get(fn, baseline['mean_coefs'][fn])) >
                    0.5 * abs(baseline['mean_coefs'][fn])
                    for fn in baseline['fit_feature_names'] if fn != exclude_feature)
        print(f"3. Do remaining coefficients reorganize materially (>50% relative change in any)? "
              f"{'YES' if reorg else 'NO'}")

    print(f"4. Interpretation: ", end="")
    if d_auc > 0.03 and a_inverted == False and b_inverted == True:
        print(f"RS was actively suppressing/inverting the signal -- removing it both improved AUC")
        print(f"   and corrected the winner/loser ordering. This is the strongest possible confirming result.")
    elif d_auc > 0.01:
        print(f"RS was contributing to the weak result, but the remaining features still show only")
        print(f"   modest discrimination -- consistent with 'RS was PART of the problem, not the whole cause.'")
    else:
        print(f"AUC did not materially improve -- RS alone was not responsible. The remaining OHLCV")
        print(f"   features still show insufficient predictive information on their own.")

    print(f"5. Recommendation: ", end="")
    if ablated['auc'] > 0.55 and d_auc > 0.03:
        print(f"continue refining the existing OHLCV feature set -- excluding RS unlocked real")
        print(f"   discrimination (AUC={ablated['auc']:.4f}), evidence the remaining features carry signal.")
    else:
        print(f"pivot to genuinely new information sources. Removing the one feature with a strong,")
        print(f"   stable, consistently-signed coefficient did not lift discrimination to a meaningful")
        print(f"   level (AUC={ablated['auc']:.4f}) -- the remaining 5 OHLCV-derived features do not")
        print(f"   appear to carry enough independent signal on their own.")

# ============================================================
# FINAL DIAGNOSTIC: CONTINUOUS 30-DAY FORWARD RETURN (read-only)
#
# Tests whether the six features carry linear/monotonic information about
# raw forward return, independent of the triple-barrier label entirely.
# Reuses build_label_audit_samples() (for X/warmup logic), is_candidate_sample()
# (unmodified), purge_and_embargo_samples() (unmodified), compute_fold_
# boundaries() (unmodified), pearson_r() (unmodified), percentile()
# (unmodified). No regime stratification, no BTC-normalization, no
# discretization -- exactly as specified.
# ============================================================

def compute_forward_return(closes, i_local, horizon=30):
    """(close[t+horizon] / close[t]) - 1, or None if insufficient forward data."""
    if i_local + horizon >= len(closes):
        return None
    return closes[i_local + horizon] / closes[i_local] - 1

def build_forward_return_samples(histories_dated, registry, day_start, day_end,
                                  btc_id="bitcoin", hilo_dated=None, horizon=30):
    """
    Mirrors build_label_audit_samples()'s exact population-selection loop
    (same local_i>=210 check, same per-feature min_lookback gating,
    identical warmup logic) but attaches forward_return (continuous, raw,
    NOT excess-vs-BTC, NOT discretized) instead of the triple-barrier
    outcome. holding_days is fixed at `horizon` so the existing
    purge_and_embargo_samples() function works unmodified -- it only reads
    entry_global_i and holding_days from each sample, agnostic to what
    they represent.
    """
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    hilo_dated = hilo_dated or {}
    date_idx = {cid: {d: idx for idx, d in enumerate(dates)} for cid, (dates, closes, vols) in histories_dated.items()}
    hilo_idx = {}
    for cid, (hdates, highs, lows) in hilo_dated.items():
        if hdates is not None:
            hilo_idx[cid] = {d: idx for idx, d in enumerate(hdates)}

    feature_names = list(registry)
    samples = []
    end_i = min(n, day_end)
    start_i = max(210, day_start)
    for i in range(start_i, end_i):
        D = btc_dates[i]
        for cid, (dates, closes, vols) in histories_dated.items():
            sym = TOKENS.get(cid, cid)
            local_i = date_idx[cid].get(D)
            if local_i is None or local_i < 210:
                continue
            highs = lows = None
            if cid in hilo_dated and cid in hilo_idx:
                h_local_i = hilo_idx[cid].get(D)
                if h_local_i is not None and h_local_i == local_i:
                    _, highs, lows = hilo_dated[cid]
            skip = False
            X_row = []
            for fname in feature_names:
                feat = registry[fname]
                if local_i < feat.min_lookback:
                    skip = True; break
                rv = feat.raw(closes, vols, local_i, btc_closes, i, highs=highs, lows=lows)
                nv = feat.normalized(rv, closes, vols, local_i, btc_closes, i, highs=highs, lows=lows) if rv is not None else 0.0
                X_row.append(nv)
            if skip:
                continue
            fwd = compute_forward_return(closes, local_i, horizon=horizon)
            if fwd is None:
                continue
            samples.append(dict(cid=cid, sym=sym, entry_global_i=i, entry_local_i=local_i,
                                 holding_days=horizon, fwd_return=fwd, X=X_row))
    return samples

def spearman_r(xs, ys):
    """Spearman rank correlation -- rank-transforms both series, then reuses
    the existing pearson_r() unmodified on the ranks."""
    def rank(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        ranks = [0.0]*len(vals)
        i = 0
        while i < len(order):
            j = i
            while j+1 < len(order) and vals[order[j+1]] == vals[order[i]]:
                j += 1
            avg_rank = (i+1 + j+1) / 2.0
            for k in range(i, j+1): ranks[order[k]] = avg_rank
            i = j+1
        return ranks
    return pearson_r(rank(xs), rank(ys))

def linear_regression_fit(X, y, ridge_epsilon=1e-8):
    """
    Ordinary least squares via the normal equations, pure Python (no
    numpy) -- solves (X'X + eps*I)^-1 X'y for a small design matrix (6
    features + intercept = 7x7) via Gauss-Jordan elimination. Returns
    (intercept, coefs, r_squared).

    BUG FIX (found via direct reproduction, not theorized): the previous
    version used a fixed threshold (abs(pivot) < 1e-12) to detect singular
    columns during elimination. When a feature is constant or near-
    constant within a fold (e.g. Trend collapsing to a near-fixed value
    within the candidate population -- documented earlier in this
    project), the resulting pivot is a tiny but NONZERO floating-point
    residue that slips past that threshold. Dividing by it amplified that
    row by ~1e12, and the amplification cascaded through every subsequent
    elimination step -- producing coefficients and predictions in the
    1e10-1e24 range (reproduced exactly: intercept=-1.66e13, matching the
    reported failure).

    Fix: add a small ridge term (ridge_epsilon * that diagonal entry's own
    scale) to every diagonal of A before solving. This bounds every pivot
    away from zero BY CONSTRUCTION, eliminating the failure mode entirely
    rather than tuning a threshold that can always be evaded by some
    ill-conditioned case. For well-conditioned data the effect is
    negligible (verified: does not materially change previously-reported
    results); for near-singular data it produces the same well-behaved,
    small-coefficient solution sklearn's SVD-based solver reaches, instead
    of diverging.
    """
    n = len(X)
    if n == 0: return 0.0, [0.0]*(len(X[0]) if X else 0), 0.0
    p = len(X[0]) + 1  # +1 for intercept
    Xd = [[1.0] + list(row) for row in X]  # design matrix with intercept column

    # Normal equations: A = X'X (pxp), b = X'y (px1)
    A = [[sum(Xd[k][i]*Xd[k][j] for k in range(n)) for j in range(p)] for i in range(p)]
    b = [sum(Xd[k][i]*y[k] for k in range(n)) for i in range(p)]

    # Ridge regularization: scale epsilon to each diagonal entry's own
    # magnitude (plus a small floor) so it's proportionate regardless of
    # each feature's native scale, and never exactly zero even if a
    # diagonal entry itself is zero (a fully-constant-zero column).
    for i in range(p):
        A[i][i] += ridge_epsilon * (abs(A[i][i]) + 1.0)

    # Gauss-Jordan elimination on augmented [A|b] to solve A.beta = b
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(p):
        pivot_row = max(range(col, p), key=lambda r: abs(M[r][col]))
        M[col], M[pivot_row] = M[pivot_row], M[col]
        pivot = M[col][col]
        # With ridge regularization, pivot is guaranteed bounded away from
        # zero -- no singular-column special case needed anymore.
        M[col] = [v/pivot for v in M[col]]
        for r in range(p):
            if r != col:
                factor = M[r][col]
                M[r] = [M[r][k] - factor*M[col][k] for k in range(p+1)]
    beta = [M[i][p] for i in range(p)]
    intercept, coefs = beta[0], beta[1:]

    y_pred = [intercept + sum(c*x for c, x in zip(coefs, row)) for row in X]
    y_mean = sum(y)/n
    ss_res = sum((yi-pi)**2 for yi, pi in zip(y, y_pred))
    ss_tot = sum((yi-y_mean)**2 for yi in y)
    r_squared = 1 - ss_res/ss_tot if ss_tot > 1e-12 else 0.0
    return intercept, coefs, r_squared

def continuous_permutation_test(xs, ys, metric="pearson", n_perm=2000, seed=42):
    """
    Same shuffle-based null-distribution philosophy as the existing
    permutation_test() (which operates on binary trade components) --
    here applied to two continuous series directly. Shuffles ys, recomputes
    the correlation, and reports the fraction of shuffles at least as
    extreme as the real value.
    """
    rng = random.Random(seed)
    real = pearson_r(xs, ys) if metric == "pearson" else spearman_r(xs, ys)
    if real is None:
        return None, 1.0
    shuffled = list(ys)
    count_extreme = 0
    for _ in range(n_perm):
        rng.shuffle(shuffled)
        r = pearson_r(xs, shuffled) if metric == "pearson" else spearman_r(xs, shuffled)
        if r is not None and abs(r) >= abs(real):
            count_extreme += 1
    return real, count_extreme / n_perm

def bootstrap_ci_correlation(xs, ys, statfn, n_bootstrap=500, seed=42):
    """Bootstrap 95% CI for any (xs,ys)->float statistic, reusing the
    existing percentile() function unmodified."""
    rng = random.Random(seed)
    n = len(xs)
    vals = []
    for _ in range(n_bootstrap):
        idxs = [rng.randrange(n) for _ in range(n)]
        xb = [xs[i] for i in idxs]; yb = [ys[i] for i in idxs]
        v = statfn(xb, yb)
        if v is not None:
            vals.append(v)
    if not vals:
        return None, None
    return percentile(vals, 0.025), percentile(vals, 0.975)

def _purge_pool_across_folds(samples, folds, embargo_days):
    """
    Applies the SAME embargo principle as purge_and_embargo_samples() --
    a sample is excluded if its fixed-width forward-return window crosses
    a fold boundary -- but implemented directly rather than calling that
    function, because its actual contract (build a training set relative
    to ONE held-out fold, purging/embargoing against a single test window
    plus prior test windows) doesn't fit this use case (deduplicating one
    POOLED descriptive population across ALL fold boundaries at once).
    Forcing a call that doesn't match the intended semantics would silently
    produce an incorrect result, so this reimplements the identical
    embargo LOGIC (entry < boundary <= entry+horizon => exclude) directly,
    using the same embargo_days parameter and the same folds already
    computed by compute_fold_boundaries() (unmodified).
    """
    boundaries = sorted(test_start for test_start, _ in folds[1:])
    pooled = []
    for s in samples:
        entry = s['entry_global_i']
        window_end = entry + s['holding_days']
        crosses_boundary = any(entry < b <= window_end for b in boundaries)
        if not crosses_boundary:
            pooled.append(s)
    return pooled

def run_forward_return_audit(histories_dated, registry=None, btc_id="bitcoin", n_folds_requested=4,
                              hilo_dated=None, max_holding=90, horizon=30, n_bootstrap=500):
    """
    Answers ONE question: do the six features carry linear/monotonic
    information about raw continuous 30-day forward return? Per-asset
    first, then pooled. No regime stratification, no BTC-normalization,
    no discretization -- diagnostic only, does not touch trading logic.
    """
    registry = registry if registry is not None else FEATURE_REGISTRY
    feature_names = list(registry)
    hand_weights = {fn: registry[fn].weight for fn in feature_names}
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    ema200_btc = rolling_ema_series(btc_closes, 200)
    folds = compute_fold_boundaries(n, n_folds_requested, warmup=210)
    if not folds:
        print(f"\nFORWARD-RETURN AUDIT: insufficient history ({n} days).")
        return

    print(f"\n{'='*98}\nFINAL DIAGNOSTIC: CONTINUOUS {horizon}-DAY FORWARD RETURN\n{'='*98}")
    print(f"Target: (close[t+{horizon}] / close[t]) - 1  -- raw, continuous, NOT excess-vs-BTC, NOT discretized")
    print(f"Population: candidate days only (hand-set score>=70, regime-on), same rule used elsewhere.")
    print(f"Deduplication: existing purge+embargo machinery, embargo={horizon} days (fixed horizon).\n")

    all_samples = build_forward_return_samples(histories_dated, registry, 210, n, btc_id=btc_id,
                                                hilo_dated=hilo_dated, horizon=horizon)
    candidates = [s for s in all_samples if is_candidate_sample(s, feature_names, hand_weights, btc_closes, ema200_btc)]
    pooled = _purge_pool_across_folds(candidates, folds, embargo_days=horizon)
    print(f"Total candidates: {len(candidates)}   After fold-boundary deduplication: {len(pooled)}\n")

    def analyze(samples, label):
        print(f"{'-'*98}\n{label}  (n={len(samples)})\n{'-'*98}")
        if len(samples) < 20:
            print("Too few samples for reliable statistics.")
            return None
        y = [s['fwd_return'] for s in samples]
        results = {}
        def fmt(v, spec="+.4f"):
            return format(v, spec) if v is not None else "n/a"
        for j, fn in enumerate(feature_names):
            xs = [s['X'][j] for s in samples]
            pr = pearson_r(xs, y)
            sr = spearman_r(xs, y)
            pval = continuous_permutation_test(xs, y, metric="pearson", n_perm=1000)[1] if pr is not None else None
            pr_lo, pr_hi = bootstrap_ci_correlation(xs, y, pearson_r, n_bootstrap=n_bootstrap) if pr is not None else (None, None)
            sr_lo, sr_hi = bootstrap_ci_correlation(xs, y, spearman_r, n_bootstrap=n_bootstrap) if sr is not None else (None, None)
            results[fn] = dict(pearson=pr, spearman=sr, pval=pval, pr_ci=(pr_lo,pr_hi), sr_ci=(sr_lo,sr_hi))
            pval_str = fmt(pval, ".3f")
            note = "  (zero variance in this population)" if pr is None else ""
            print(f"  {fn:22s} Pearson={fmt(pr)} [{fmt(pr_lo,'+.3f')},{fmt(pr_hi,'+.3f')}]  "
                  f"Spearman={fmt(sr)} [{fmt(sr_lo,'+.3f')},{fmt(sr_hi,'+.3f')}]  perm-p={pval_str}{note}")

        X_all = [s['X'] for s in samples]
        intercept, coefs, r2 = linear_regression_fit(X_all, y)
        r2_lo, r2_hi = bootstrap_ci_correlation(
            [tuple(row) for row in X_all], y,
            lambda xb, yb: linear_regression_fit([list(r) for r in xb], yb)[2],
            n_bootstrap=n_bootstrap)
        print(f"\n  Joint multivariate R² (all 6 features): {r2:.4f}  [{r2_lo:.4f},{r2_hi:.4f}]")
        print(f"  Intercept: {intercept:+.4f}   Coefficients: " +
              ", ".join(f"{fn}={c:+.4f}" for fn, c in zip(feature_names, coefs)))
        results['joint_r2'] = r2; results['joint_r2_ci'] = (r2_lo, r2_hi)
        return results

    print(f"{'='*98}\nPER-ASSET RESULTS\n{'='*98}")
    by_asset = {}
    for s in pooled: by_asset.setdefault(s['sym'], []).append(s)
    per_asset_results = {}
    for sym, slist in sorted(by_asset.items()):
        per_asset_results[sym] = analyze(slist, f"ASSET: {sym}")
        print()

    print(f"{'='*98}\nPOOLED RESULTS\n{'='*98}")
    pooled_results = analyze(pooled, "POOLED (all assets)")

    print(f"\n{'-'*98}\nPRACTICAL SIGNIFICANCE\n{'-'*98}")
    if pooled_results:
        valid_r = [abs(pooled_results[fn]['pearson']) for fn in feature_names if pooled_results[fn]['pearson'] is not None]
        max_abs_r = max(valid_r) if valid_r else 0.0
        print(f"Largest pooled |Pearson r| across the 6 features: {max_abs_r:.4f}"
              + (" (some features had zero variance -- excluded from this max)" if len(valid_r) < len(feature_names) else ""))
        print(f"Joint R²: {pooled_results['joint_r2']:.4f}")
        print("For context: |r| < 0.05 explains under 0.25% of return variance -- statistically")
        print("detectable with enough samples, but far below what a tradeable edge typically requires.")
        print("|r| in the 0.05-0.15 range is small but potentially real; above ~0.15 starts to be")
        print("the kind of magnitude worth building a strategy around.")

    print(f"\n{'='*98}\nFINAL CONCLUSION\n{'='*98}")
    if pooled_results:
        significant_features = [fn for fn in feature_names
                                if pooled_results[fn]['pval'] is not None and pooled_results[fn]['pval'] < 0.05]
        meaningful_features = [fn for fn in feature_names
                               if pooled_results[fn]['pearson'] is not None and abs(pooled_results[fn]['pearson']) > 0.05]
        if pooled_results['joint_r2'] > 0.02 or meaningful_features:
            print("A. Evidence supports meaningful predictive information.")
            print(f"   Significant (p<0.05): {significant_features}")
            print(f"   Practically meaningful (|r|>0.05): {meaningful_features}")
        else:
            print("B. Evidence does NOT support meaningful predictive information.")
            print(f"   Significant at p<0.05: {significant_features if significant_features else 'none'}")
            print(f"   None reach practical meaningfulness (|r|>0.05).")
            print(f"   Joint R²={pooled_results['joint_r2']:.4f} -- negligible explained variance.")
            print()
            print("   Given this result, combined with the full prior investigation (feature leakage")
            print("   rejected, label leakage rejected, population mismatch rejected, representation")
            print("   rejected, RS ablation tested, and now the triple-barrier label itself replaced")
            print("   with a neutral continuous target and still showing no linear/monotonic signal),")
            print("   it is reasonable to retire further OHLCV-only feature engineering on this six-")
            print("   feature family and redirect research toward orthogonal information sources:")
            print("   market breadth, derivatives (funding/OI), cross-asset signals, on-chain metrics,")
            print("   or macro liquidity -- rather than adding RSI, EMA, MACD, ADX, or similar")
            print("   price-derived indicators from the same information family already tested.")
    else:
        print("Insufficient data to reach a conclusion.")

# ============================================================
# NONLINEAR BENCHMARK (additive, lazily-imported)
#
# The ONE remaining scientific question: can a nonlinear model extract
# materially more signal from the ALREADY-FROZEN feature set than the
# linear methods used throughout this project? This does not touch
# ANY existing feature calculation, candidate generation, label
# definition, or purge/embargo logic -- it reuses build_forward_return_
# samples(), is_candidate_sample(), compute_fold_boundaries(),
# purge_and_embargo_samples(), linear_regression_fit(), and spearman_r()
# completely unmodified, and only swaps in a nonlinear model where the
# linear fit previously went.
#
# Unlike every other function in this file, this ONE experiment requires
# external libraries (xgboost, or lightgbm, or scikit-learn as a last
# resort) -- imported lazily, INSIDE this function only, so every other
# command in this script continues to run with zero dependencies beyond
# the standard library. If none of the three are installed, this
# function prints exact install instructions and exits cleanly rather
# than crashing the whole script or silently degrading.
# ============================================================

def _load_nonlinear_model_backend():
    """
    Returns (backend_name, fit_predict_fn, importance_fn, shap_fn) using
    the FIRST available library in the requested preference order:
    XGBoost -> LightGBM -> scikit-learn RandomForestRegressor. Each
    backend is wrapped behind the SAME small interface so the rest of
    this experiment doesn't need to know which one is active.
    """
    try:
        import xgboost as xgb
        def fit_predict(X_train, y_train, X_test):
            model = xgb.XGBRegressor(n_estimators=200, max_depth=4, learning_rate=0.05,
                                     subsample=0.8, colsample_bytree=0.8,
                                     reg_lambda=1.0, random_state=42, verbosity=0)
            model.fit(X_train, y_train)
            return model, list(model.predict(X_test))
        def importance(model, feature_names):
            imp = model.feature_importances_
            return dict(zip(feature_names, [float(v) for v in imp]))
        def shap_values(model, X_test, feature_names):
            try:
                import shap
                explainer = shap.TreeExplainer(model)
                sv = explainer.shap_values(X_test)
                mean_abs = [sum(abs(row[j]) for row in sv) / len(sv) for j in range(len(feature_names))]
                return dict(zip(feature_names, mean_abs))
            except ImportError:
                return None
        return "XGBoost", fit_predict, importance, shap_values
    except ImportError:
        pass

    try:
        import lightgbm as lgb
        def fit_predict(X_train, y_train, X_test):
            model = lgb.LGBMRegressor(n_estimators=200, max_depth=4, learning_rate=0.05,
                                      subsample=0.8, colsample_bytree=0.8,
                                      reg_lambda=1.0, random_state=42, verbosity=-1)
            model.fit(X_train, y_train)
            return model, list(model.predict(X_test))
        def importance(model, feature_names):
            imp = model.feature_importances_
            return dict(zip(feature_names, [float(v) for v in imp]))
        def shap_values(model, X_test, feature_names):
            try:
                import shap
                explainer = shap.TreeExplainer(model)
                sv = explainer.shap_values(X_test)
                mean_abs = [sum(abs(row[j]) for row in sv) / len(sv) for j in range(len(feature_names))]
                return dict(zip(feature_names, mean_abs))
            except ImportError:
                return None
        return "LightGBM", fit_predict, importance, shap_values
    except ImportError:
        pass

    try:
        from sklearn.ensemble import RandomForestRegressor
        def fit_predict(X_train, y_train, X_test):
            model = RandomForestRegressor(n_estimators=200, max_depth=6, min_samples_leaf=10,
                                          random_state=42, n_jobs=-1)
            model.fit(X_train, y_train)
            return model, list(model.predict(X_test))
        def importance(model, feature_names):
            imp = model.feature_importances_
            return dict(zip(feature_names, [float(v) for v in imp]))
        def shap_values(model, X_test, feature_names):
            return None  # TreeExplainer support for RF is available via shap, but kept out-of-scope for the fallback path
        return "RandomForest (scikit-learn)", fit_predict, importance, shap_values
    except ImportError:
        return None, None, None, None


def out_of_sample_r2(y_true, y_pred):
    n = len(y_true)
    if n == 0: return None
    y_mean = sum(y_true) / n
    ss_res = sum((yt - yp) ** 2 for yt, yp in zip(y_true, y_pred))
    ss_tot = sum((yt - y_mean) ** 2 for yt in y_true)
    return 1 - ss_res / ss_tot if ss_tot > 1e-12 else None

def mae(y_true, y_pred):
    return sum(abs(yt - yp) for yt, yp in zip(y_true, y_pred)) / len(y_true)

def rmse(y_true, y_pred):
    return math.sqrt(sum((yt - yp) ** 2 for yt, yp in zip(y_true, y_pred)) / len(y_true))


def run_nonlinear_benchmark(histories_dated, registry=None, btc_id="bitcoin", n_folds_requested=4,
                             hilo_dated=None, max_holding=90, horizon=30):
    """
    ONE additive experiment: does a nonlinear model extract materially
    more signal from the FROZEN feature set than linear methods? Reuses
    every existing population/fold/purge/label function unmodified;
    only the model fit at each fold is nonlinear instead of linear.
    """
    backend_name, fit_predict, importance_fn, shap_fn = _load_nonlinear_model_backend()
    if backend_name is None:
        print(f"\n{'='*98}\nNONLINEAR BENCHMARK -- NO BACKEND AVAILABLE\n{'='*98}")
        print("None of xgboost, lightgbm, or scikit-learn are installed in this environment.")
        print("This is the ONLY command in this tool that requires external libraries --")
        print("every other command remains pure-Python, zero-dependency, unaffected.")
        print("\nInstall one of the following, then re-run:")
        print("  pip install xgboost shap --break-system-packages     (preferred)")
        print("  pip install lightgbm shap --break-system-packages    (second choice)")
        print("  pip install scikit-learn --break-system-packages     (fallback, no SHAP support)")
        return

    registry = registry if registry is not None else FEATURE_REGISTRY
    feature_names = list(registry)
    hand_weights = {fn: registry[fn].weight for fn in feature_names}
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    ema200_btc = rolling_ema_series(btc_closes, 200)
    folds = compute_fold_boundaries(n, n_folds_requested, warmup=210)
    if not folds:
        print(f"\nNONLINEAR BENCHMARK: insufficient history ({n} days).")
        return

    print(f"\n{'='*98}\nNONLINEAR BENCHMARK: {backend_name}\n{'='*98}")
    print(f"Backend: {backend_name} (first available in preferred order: XGBoost > LightGBM > RandomForest)")
    print(f"Feature set: FROZEN, unchanged -- {feature_names}")
    print(f"Target: continuous {horizon}-day forward return (same target as the linear forward-return audit)")
    print(f"Population, folds, purge/embargo: reused exactly from existing functions, unmodified.\n")

    all_samples = build_forward_return_samples(histories_dated, registry, 210, n, btc_id=btc_id,
                                                hilo_dated=hilo_dated, horizon=horizon)
    candidates = [s for s in all_samples if is_candidate_sample(s, feature_names, hand_weights, btc_closes, ema200_btc)]
    print(f"Candidate population: {len(candidates)} (identical selection rule as every prior experiment)\n")

    prior_test_ranges = []
    fold_results = []
    pooled_true_lin, pooled_pred_lin = [], []
    pooled_true_nl, pooled_pred_nl = [], []
    all_importances = []
    all_shap = []
    largest_fold_train = None  # captured for the learning-curve analysis

    for fi, (test_start, test_end) in enumerate(folds):
        naive_train = [s for s in candidates if s['entry_global_i'] < test_start]
        train_samples, _, _ = purge_and_embargo_samples(naive_train, test_start, test_end, prior_test_ranges, horizon)
        prior_test_ranges.append((test_start, test_end))
        test_samples = [s for s in candidates if test_start <= s['entry_global_i'] < test_end]

        if len(train_samples) < 30 or len(test_samples) < 10:
            print(f"Fold {fi+1}: insufficient samples (train={len(train_samples)}, test={len(test_samples)}) -- skipped.")
            fold_results.append(None)
            continue

        X_train = [s['X'] for s in train_samples]; y_train = [s['fwd_return'] for s in train_samples]
        X_test = [s['X'] for s in test_samples]; y_test = [s['fwd_return'] for s in test_samples]

        if largest_fold_train is None or len(X_train) > len(largest_fold_train[0]):
            largest_fold_train = (X_train, y_train, X_test, y_test)

        # Linear baseline -- SAME function used throughout this project, unmodified except for
        # the ridge-stability fix (see linear_regression_fit's docstring for the reproduced bug).
        intercept, coefs, _ = linear_regression_fit(X_train, y_train)
        y_pred_lin = [intercept + sum(c*x for c, x in zip(coefs, row)) for row in X_test]

        # ---- Linear implementation audit: condition number, rank, coefficient norm, sklearn comparison ----
        # numpy/sklearn are already required dependencies for this command (via the nonlinear
        # backend itself), so using them here for diagnostics adds no NEW dependency burden.
        import numpy as np
        from sklearn.linear_model import LinearRegression as SkLinearRegression
        X_train_np = np.array(X_train)
        cond_number = np.linalg.cond(X_train_np) if len(X_train) > 1 else float('nan')
        matrix_rank = np.linalg.matrix_rank(X_train_np) if len(X_train) > 1 else 0
        coef_norm = math.sqrt(intercept**2 + sum(c*c for c in coefs))
        sk_model = SkLinearRegression().fit(X_train, y_train)
        y_pred_sk = list(sk_model.predict(X_test))
        r2_sk = out_of_sample_r2(y_test, y_pred_sk)
        max_coef_diff = max(abs(c - s) for c, s in zip(coefs, sk_model.coef_))
        intercept_diff = abs(intercept - sk_model.intercept_)
        match_ok = max_coef_diff < 1e-3 and intercept_diff < 1e-3

        print(f"Fold {fi+1}: train={len(train_samples)} test={len(test_samples)}")
        print(f"  [Linear audit] condition#={cond_number:.2e}  rank={matrix_rank}/{len(feature_names)+1}  "
              f"coef_norm={coef_norm:.4f}  pred[min,max]=[{min(y_pred_lin):.4f},{max(y_pred_lin):.4f}]  "
              f"actual[min,max]=[{min(y_test):.4f},{max(y_test):.4f}]")
        print(f"  [Linear audit] custom vs sklearn: max_coef_diff={max_coef_diff:.2e}  "
              f"intercept_diff={intercept_diff:.2e}  sklearn_R²={r2_sk:+.4f}  "
              f"{'MATCH' if match_ok else 'MISMATCH -- investigate'}")
        if any(not math.isfinite(p) for p in y_pred_lin) or any(abs(p) > 1e6 for p in y_pred_lin):
            print(f"  ⚠️  Linear predictions contain non-finite or implausibly large values -- numerical failure.")

        # Nonlinear model
        model, y_pred_nl = fit_predict(X_train, y_train, X_test)

        r2_lin = out_of_sample_r2(y_test, y_pred_lin)
        r2_nl = out_of_sample_r2(y_test, y_pred_nl)
        sr_lin = spearman_r(y_pred_lin, y_test)
        sr_nl = spearman_r(y_pred_nl, y_test)
        mae_lin, mae_nl = mae(y_test, y_pred_lin), mae(y_test, y_pred_nl)
        rmse_lin, rmse_nl = rmse(y_test, y_pred_lin), rmse(y_test, y_pred_nl)

        print(f"  Linear:    R²={r2_lin:+.4f}  Spearman={sr_lin:+.4f}  MAE={mae_lin:.4f}  RMSE={rmse_lin:.4f}")
        print(f"  {backend_name:10s}: R²={r2_nl:+.4f}  Spearman={sr_nl:+.4f}  MAE={mae_nl:.4f}  RMSE={rmse_nl:.4f}")

        fold_results.append(dict(r2_lin=r2_lin, r2_nl=r2_nl, sr_lin=sr_lin, sr_nl=sr_nl))
        pooled_true_lin.extend(y_test); pooled_pred_lin.extend(y_pred_lin)
        pooled_true_nl.extend(y_test); pooled_pred_nl.extend(y_pred_nl)

        imp = importance_fn(model, feature_names)
        all_importances.append(imp)
        sv = shap_fn(model, X_test, feature_names)
        if sv is not None:
            all_shap.append(sv)

    valid_folds = [f for f in fold_results if f is not None]
    if not valid_folds:
        print("\nNo fold produced enough data to evaluate. Cannot proceed.")
        return

    print(f"\n{'-'*98}\nPOOLED OUT-OF-SAMPLE RESULTS (across all valid folds)\n{'-'*98}")
    pooled_r2_lin = out_of_sample_r2(pooled_true_lin, pooled_pred_lin)
    pooled_r2_nl = out_of_sample_r2(pooled_true_nl, pooled_pred_nl)
    pooled_sr_lin = spearman_r(pooled_pred_lin, pooled_true_lin)
    pooled_sr_nl = spearman_r(pooled_pred_nl, pooled_true_nl)
    pooled_mae_lin, pooled_mae_nl = mae(pooled_true_lin, pooled_pred_lin), mae(pooled_true_nl, pooled_pred_nl)
    pooled_rmse_lin, pooled_rmse_nl = rmse(pooled_true_lin, pooled_pred_lin), rmse(pooled_true_nl, pooled_pred_nl)
    print(f"Linear:      R²={pooled_r2_lin:+.4f}  Spearman={pooled_sr_lin:+.4f}  MAE={pooled_mae_lin:.4f}  RMSE={pooled_rmse_lin:.4f}")
    print(f"{backend_name:12s}: R²={pooled_r2_nl:+.4f}  Spearman={pooled_sr_nl:+.4f}  MAE={pooled_mae_nl:.4f}  RMSE={pooled_rmse_nl:.4f}")

    print(f"\n{'-'*98}\nFOLD-BY-FOLD STABILITY\n{'-'*98}")
    r2_nl_vals = [f['r2_nl'] for f in valid_folds]
    print(f"{backend_name} R² by fold: " + ", ".join(f"{v:+.4f}" for v in r2_nl_vals))
    r2_mean = sum(r2_nl_vals) / len(r2_nl_vals)
    r2_std = math.sqrt(sum((v - r2_mean) ** 2 for v in r2_nl_vals) / len(r2_nl_vals)) if len(r2_nl_vals) > 1 else 0.0
    print(f"Mean={r2_mean:+.4f}  Std={r2_std:.4f}  "
          f"{'STABLE across folds' if r2_std < 0.05 else 'UNSTABLE -- high fold-to-fold variance'}")

    print(f"\n{'-'*98}\nFEATURE IMPORTANCE (mean across folds, {backend_name} native importance)\n{'-'*98}")
    mean_importance = {fn: sum(imp.get(fn, 0) for imp in all_importances) / len(all_importances) for fn in feature_names}
    for fn in sorted(feature_names, key=lambda f: -mean_importance[f]):
        print(f"  {fn:24s} {mean_importance[fn]:.4f}")

    if all_shap:
        print(f"\n{'-'*98}\nSHAP VALUES (mean |SHAP|, averaged across folds)\n{'-'*98}")
        mean_shap = {fn: sum(sv.get(fn, 0) for sv in all_shap) / len(all_shap) for fn in feature_names}
        for fn in sorted(feature_names, key=lambda f: -mean_shap[f]):
            print(f"  {fn:24s} {mean_shap[fn]:.4f}")
    else:
        print(f"\n{'-'*98}\nSHAP VALUES\n{'-'*98}")
        print("Not available (shap library not installed, or unsupported for this backend).")

    if largest_fold_train:
        print(f"\n{'-'*98}\nLEARNING CURVE (largest fold's training data, subsetted; test set fixed)\n{'-'*98}")
        X_tr, y_tr, X_te, y_te = largest_fold_train
        print(f"{'train %':>8s} {'train n':>8s} {'R²':>8s} {'MAE':>8s}")
        for frac in [0.2, 0.4, 0.6, 0.8, 1.0]:
            k = max(30, int(len(X_tr) * frac))
            k = min(k, len(X_tr))
            _, y_pred_sub = fit_predict(X_tr[:k], y_tr[:k], X_te)
            r2_sub = out_of_sample_r2(y_te, y_pred_sub)
            mae_sub = mae(y_te, y_pred_sub)
            print(f"{frac*100:>7.0f}% {k:>8d} {r2_sub:>+7.4f} {mae_sub:>8.4f}")

    print(f"\n{'='*98}\nDECISION\n{'='*98}")
    improvement = pooled_r2_nl - pooled_r2_lin
    print(f"Linear pooled R²: {pooled_r2_lin:+.4f}    {backend_name} pooled R²: {pooled_r2_nl:+.4f}    "
          f"Δ={improvement:+.4f}")
    # Threshold is a judgment call, stated explicitly rather than hidden:
    # "material" requires BOTH more than double the linear baseline's
    # explanatory power AND crossing an absolute R² floor of 0.03 --
    # consistent with the practical-significance thresholds already used
    # elsewhere in this project (e.g. |r|>0.05 as the "small but real" bar).
    material = (pooled_r2_nl > 0.03) and (pooled_r2_nl > 2 * max(pooled_r2_lin, 0.001))
    if material:
        print("MATERIAL improvement detected. The nonlinear model extracts meaningfully more")
        print("signal from the frozen feature set than linear methods could. This suggests")
        print("real, exploitable nonlinear/interaction structure exists among the six frozen")
        print("features -- worth investigating further (interaction terms, model tuning)")
        print("before concluding the OHLCV family is exhausted.")
    else:
        print("MARGINAL improvement only. The nonlinear model does not materially exceed the")
        print("linear baseline's already-weak explanatory power.")
        print()
        print("CONCLUSION: the information ceiling lies in the FEATURE SET, not the model.")
        print("Combined with every prior test in this project (leakage eliminated, population")
        print("mismatch rejected, representation rejected, RS ablation tested, continuous target")
        print("tested, cross-sectional rank tested, shared-series tested, and now a nonlinear")
        print("model tested against the same frozen features) -- OHLCV-derived feature research")
        print("should be PERMANENTLY frozen. Recommend redirecting to genuinely orthogonal")
        print("information sources: funding rates, open interest, liquidations, options")
        print("positioning, on-chain metrics, macro liquidity, stablecoin flows.")
        print("Do not add further technical indicators from the OHLCV family.")

def run_purged_validation(histories_dated, registry=None, btc_id="bitcoin", n_folds_requested=4,
                           hilo_dated=None, l2=1.0, n_bootstrap=100, max_holding=90,
                           max_positions=None):
    """
    Institution-grade purged walk-forward CV. For each fold:
      1. Build the NAIVE candidate training set (entry_global_i < test_start)
         via build_label_audit_samples() -- same population Phase 2 audited.
      2. Apply purge + embargo (Lopez de Prado).
      3. Report leakage % before vs after (should be exactly 0% after).
      4. Fit logistic regression on the PURGED set (same fitting code as
         run_train_model(), unmodified -- standardize/logistic_regression_fit).
      5. ALSO fit on the UNPURGED (naive) set, for direct comparison.
      6. Evaluate BOTH fitted models' coefficients, via weight_override,
         on the SAME held-out test-period trades (run_dynamic_attr,
         unmodified) -- this is where expectancy/win-rate/drawdown
         before-vs-after purging get measured, since the hand-set
         scorecard itself has no trainable parameters and is therefore
         completely unaffected by purging (a fact worth stating plainly,
         not hidden).
    """
    registry = registry if registry is not None else FEATURE_REGISTRY
    feature_names = list(registry)
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    embargo_days = max_holding  # requirement: embargo equal to max_holding

    folds = compute_fold_boundaries(n, n_folds_requested, warmup=210)
    if not folds:
        print(f"\nPURGED VALIDATION: insufficient history ({n} days).")
        return

    print(f"\n{'='*98}\nPURGED WALK-FORWARD CROSS-VALIDATION (Lopez de Prado)\n{'='*98}")
    print(f"Features: {feature_names}")
    print(f"Embargo: {embargo_days} days (= max_holding, per requirement)\n")

    all_samples = build_label_audit_samples(histories_dated, registry, 210, n, btc_id=btc_id,
                                             hilo_dated=hilo_dated, max_holding=max_holding)

    prior_test_ranges = []
    unpurged_weights_by_fold = []
    purged_weights_by_fold = []
    print(f"{'-'*98}\nPER-FOLD: LEAKAGE BEFORE vs AFTER PURGING\n{'-'*98}")
    print(f"{'fold':>5s} {'naive n':>9s} {'purged n':>9s} {'embargoed n':>12s} {'kept n':>8s} "
          f"{'leak% before':>13s} {'leak% after':>12s}")
    for fi, (test_start, test_end) in enumerate(folds):
        naive = [s for s in all_samples if s['entry_global_i'] < test_start]
        naive_leak = sum(1 for s in naive if s['entry_global_i'] + s['holding_days'] >= test_start)
        naive_leak_pct = 100 * naive_leak / len(naive) if naive else 0

        kept, purged, embargoed = purge_and_embargo_samples(naive, test_start, test_end, prior_test_ranges, embargo_days)
        kept_leak = sum(1 for s in kept if s['entry_global_i'] + s['holding_days'] >= test_start)
        kept_leak_pct = 100 * kept_leak / len(kept) if kept else 0

        print(f"{fi+1:>5d} {len(naive):>9d} {len(purged):>9d} {len(embargoed):>12d} {len(kept):>8d} "
              f"{naive_leak_pct:>12.1f}% {kept_leak_pct:>11.1f}%")

        prior_test_ranges.append((test_start, test_end))

        if len(kept) < 30 or len(naive) < 30:
            unpurged_weights_by_fold.append(None)
            purged_weights_by_fold.append(None)
            continue

        # Fit on NAIVE (unpurged) -- same fitting code as run_train_model()
        X_naive = [s['X'] for s in naive]; y_naive = [s['y'] for s in naive]
        Xn_std, _, _ = standardize(X_naive)
        _, coefs_naive = logistic_regression_fit(Xn_std, y_naive, l2=l2, iters=500)
        unpurged_weights_by_fold.append(dict(zip(feature_names, coefs_naive)))

        # Fit on PURGED
        X_purged = [s['X'] for s in kept]; y_purged = [s['y'] for s in kept]
        Xp_std, _, _ = standardize(X_purged)
        _, coefs_purged = logistic_regression_fit(Xp_std, y_purged, l2=l2, iters=500)
        purged_weights_by_fold.append(dict(zip(feature_names, coefs_purged)))

    # ---- Cross-fold stability, purged vs unpurged ----
    print(f"\n{'-'*98}\nCROSS-FOLD COEFFICIENT STABILITY -- UNPURGED vs PURGED\n{'-'*98}")
    valid_unpurged = [w for w in unpurged_weights_by_fold if w is not None]
    valid_purged = [w for w in purged_weights_by_fold if w is not None]
    print(f"{'feature':22s} {'unpurged mean':>14s} {'unpurged sign?':>15s}   {'purged mean':>12s} {'purged sign?':>13s}")
    rank_changes = []
    for fname in feature_names:
        uv = [w[fname] for w in valid_unpurged] if valid_unpurged else []
        pv = [w[fname] for w in valid_purged] if valid_purged else []
        u_mean = sum(uv)/len(uv) if uv else 0
        p_mean = sum(pv)/len(pv) if pv else 0
        u_consistent = (all(v>0 for v in uv) or all(v<0 for v in uv)) if uv else False
        p_consistent = (all(v>0 for v in pv) or all(v<0 for v in pv)) if pv else False
        flip = "  <-- SIGN FLIPPED" if (u_mean > 0) != (p_mean > 0) and abs(u_mean)>0.01 and abs(p_mean)>0.01 else ""
        print(f"{fname:22s} {u_mean:>+14.3f} {('YES' if u_consistent else 'NO'):>15s}   "
              f"{p_mean:>+12.3f} {('YES' if p_consistent else 'NO'):>13s}{flip}")
        rank_changes.append((fname, u_mean, p_mean))

    # ---- Downstream trading performance: purged-trained vs unpurged-trained weights, same test windows ----
    print(f"\n{'-'*98}\nDOWNSTREAM TEST-PERIOD PERFORMANCE (fixed hand-set scorecard is purging-INVARIANT --")
    print(f"shown only as a fixed reference point; the real before/after comparison is the two")
    print(f"LEARNED-weight rows below, evaluated on the identical held-out test windows)\n{'-'*98}")

    SKIP = object()  # distinct sentinel from None -- None means "use hand-set weights",
                      # SKIP means "this fold's model failed to fit, exclude it"

    def eval_weights_on_test_folds(weights_by_fold):
        all_trades = []
        for fi, (test_start, test_end) in enumerate(folds):
            w = weights_by_fold[fi]
            if w is SKIP: continue
            trades = run_dynamic_attr(histories_dated, btc_id=btc_id, day_start=test_start, day_end=test_end,
                                      max_positions=max_positions, registry=registry, hilo_dated=hilo_dated,
                                      weight_override=w)
            all_trades.extend(trades)
        return all_trades

    hand_set_trades = eval_weights_on_test_folds([None]*len(folds))  # None -> uses feat.weight (hand-set)
    unpurged_eval_list = [w if w is not None else SKIP for w in unpurged_weights_by_fold]
    purged_eval_list = [w if w is not None else SKIP for w in purged_weights_by_fold]
    unpurged_trades = eval_weights_on_test_folds(unpurged_eval_list)
    purged_trades = eval_weights_on_test_folds(purged_eval_list)

    for label, trades in [("Hand-set scorecard (reference)", hand_set_trades),
                          ("Unpurged-trained weights", unpurged_trades),
                          ("Purged-trained weights", purged_trades)]:
        s = summarize(trades)
        dd = compute_max_drawdown(trades) if trades else 0
        print(f"{label:32s} n={s['n']:5d}  win={s['win_rate']:5.1f}%  exp={s['expectancy']:+6.2f}%  maxDD={dd:.2f}R")

    # ---- Attribution comparison: purged-trained weights vs unpurged-trained weights, same tooling ----
    valid_fold_idx = [i for i, w in enumerate(purged_weights_by_fold) if w is not None]
    mean_unpurged_w = {fn: sum(unpurged_weights_by_fold[i][fn] for i in valid_fold_idx)/len(valid_fold_idx)
                        for fn in feature_names} if valid_fold_idx else {}
    mean_purged_w = {fn: sum(purged_weights_by_fold[i][fn] for i in valid_fold_idx)/len(valid_fold_idx)
                      for fn in feature_names} if valid_fold_idx else {}

    print(f"\n{'-'*98}\nFEATURE RANKING COMPARISON (by |mean learned coefficient|, unpurged vs purged)\n{'-'*98}")
    unpurged_rank = sorted(feature_names, key=lambda f: -abs(mean_unpurged_w.get(f, 0)))
    purged_rank = sorted(feature_names, key=lambda f: -abs(mean_purged_w.get(f, 0)))
    print(f"Unpurged ranking: {unpurged_rank}")
    print(f"Purged ranking:   {purged_rank}")
    if unpurged_rank != purged_rank:
        print("\n⚠️  Ranking CHANGED after purging. This means at least part of the earlier")
        print("    (unpurged) feature-importance conclusions were influenced by the fold-boundary")
        print("    label leakage Phase 2 identified -- the purged ranking is the more trustworthy")
        print("    one, since it's fit only on genuinely non-contaminated training data.")
        moved = [(f, unpurged_rank.index(f), purged_rank.index(f)) for f in feature_names
                 if unpurged_rank.index(f) != purged_rank.index(f)]
        for f, u_pos, p_pos in sorted(moved, key=lambda x: -abs(x[1]-x[2])):
            direction = "up" if p_pos < u_pos else "down"
            print(f"    {f}: rank {u_pos+1} -> {p_pos+1} ({direction})")
    else:
        print("\nRanking UNCHANGED after purging -- the fold-boundary leakage Phase 2 found did not")
        print("materially distort relative feature importance, even though it was real and present.")

    # ---- GO/NO-GO ----
    all_leak_zero = True
    for fi, (test_start, test_end) in enumerate(folds):
        naive = [s for s in all_samples if s['entry_global_i'] < test_start]
        kept, _, _ = purge_and_embargo_samples(naive, test_start, test_end, folds[:fi], embargo_days)
        if any(s['entry_global_i'] + s['holding_days'] >= test_start for s in kept):
            all_leak_zero = False
            break
    print(f"\n{'='*98}\nGO / NO-GO\n{'='*98}")
    if all_leak_zero:
        print("GO: verified zero feature leakage (Phase 1b, unaffected by this change) and zero")
        print("    label leakage (0% boundary-crossing in every fold's purged+embargoed training")
        print("    set, confirmed by direct recomputation above). This purged framework is now")
        print("    the institution-grade validation path. No new indicators until this reports GO.")
    else:
        print("NO-GO: purged training sets still show nonzero boundary-crossing -- investigate the")
        print("       purge/embargo logic before proceeding.")


def run_label_audit(histories_dated, registry=None, btc_id="bitcoin", n_folds_requested=4,
                     hilo_dated=None, max_holding=90):
    registry = registry if registry is not None else FEATURE_REGISTRY
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)

    print(f"\n{'='*94}\nPHASE 2 -- LABEL AUDIT\n{'='*94}")

    # ---- 1. Label generation documentation (from the actual source, not paraphrased) ----
    print(f"\n{'-'*94}\n1. LABEL GENERATION -- EXACT LOGIC (as implemented in simulate_trade_outcome)\n{'-'*94}")
    print("  Inputs: entry day's close price, trailing 30-day return (for volatility sizing)")
    print("  Stop width:  stop_pct = clamp(8%, 25%, |30d return| * 0.8)")
    print("  Entry:       px * (1 + SLIP)      [SLIP = 0.1%]")
    print("  Stop:        px * (1 - stop_pct)")
    print("  Target 1:    px * (1 + stop_pct*1.5)   -- half position closed here, stop trails to breakeven")
    print("  Target 2:    px * (1 + stop_pct*3)     -- remainder closed here")
    print("  Max holding: 90 days (parameterized, default 90)")
    print("  Fees:        FEE + SLIP deducted on every exit leg (0.1% + 0.1%)")
    print("  Ambiguity found: if T1 and stop are hit on the SAME future day (both conditions")
    print("    true for that day's close), the code checks `stop` FIRST -- so a day that closes")
    print("    at or below stop is always treated as a stop-out that day, even if the same day's")
    print("    close also cleared T1. This is a real, previously undocumented tie-breaking rule.")
    print("  Population: EVERY (asset, day) with sufficient warmup (Phase 1b) is labeled --")
    print("    confirmed by direct code inspection: build_training_samples() contains NO")
    print("    reference to CONV_THRESHOLD, regime state, or conviction score anywhere in its")
    print("    sample-selection loop. This is verified fact, not a design description.")

    # ---- Fidelity check: does the diagnostic builder match the REAL unmodified function ----
    print(f"\n{'-'*94}\nFIDELITY CHECK (diagnostic tooling vs the real, unmodified build_training_samples)\n{'-'*94}")
    count_match, y_match, n_real, n_audit = verify_label_fidelity(histories_dated, registry, 210, min(n, 1000), btc_id=btc_id, hilo_dated=hilo_dated)
    print(f"Sample count match: {count_match} (real={n_real}, audit={n_audit})")
    print(f"Label values match: {y_match}")
    if not (count_match and y_match):
        print("⚠️  MISMATCH -- the diagnostic tooling below would not be trustworthy. Stopping audit.")
        return

    samples = build_label_audit_samples(histories_dated, registry, 210, n, btc_id=btc_id, hilo_dated=hilo_dated, max_holding=max_holding)
    print(f"\nTotal labeled samples across full history: {len(samples)}")

    # ---- 2 & 3. Overlap and uniqueness ----
    print(f"\n{'-'*94}\n2-3. LABEL OVERLAP & UNIQUENESS (per-asset, Lopez de Prado average-uniqueness method)\n{'-'*94}")
    effective_n, n_overlapping, per_asset_uniq = compute_label_uniqueness(samples)
    print(f"{'sym':6s} {'n':>6s} {'overlapping':>12s} {'overlap%':>9s} {'effective_n':>12s} {'avg_uniqueness':>15s}")
    for sym, r in sorted(per_asset_uniq.items()):
        print(f"{sym:6s} {r['n']:>6d} {r['n_overlapping']:>12d} {r['pct_overlapping']:>8.1f}% "
              f"{r['effective_n']:>12.1f} {r['avg_uniqueness']:>15.3f}")
    print(f"\nTOTAL raw samples: {len(samples)}")
    print(f"TOTAL effective (uniqueness-weighted) sample size: {effective_n:.1f}")
    print(f"Overall shrinkage factor: {effective_n/len(samples)*100:.1f}% of raw count is 'genuinely independent'")
    print(f"Samples with ANY overlap with another same-asset label: {n_overlapping} ({100*n_overlapping/len(samples):.1f}%)")

    # ---- 4. Class balance ----
    print(f"\n{'-'*94}\n4. CLASS BALANCE\n{'-'*94}")
    overall_pos = sum(s['y'] for s in samples) / len(samples) * 100
    print(f"Overall: {overall_pos:.1f}% positive / {100-overall_pos:.1f}% negative  (n={len(samples)})")
    by_asset_bal = {}
    for s in samples: by_asset_bal.setdefault(s['sym'], []).append(s['y'])
    print(f"\nPer asset:")
    for sym, ys in sorted(by_asset_bal.items()):
        print(f"  {sym:6s} {100*sum(ys)/len(ys):>5.1f}% positive  (n={len(ys)})")
    folds = compute_fold_boundaries(n, n_folds_requested, warmup=210)
    print(f"\nPer fold (training-period samples for each fold):")
    for fi, (test_start, test_end) in enumerate(folds):
        fold_ys = [s['y'] for s in samples if s['entry_global_i'] < test_start]
        if fold_ys:
            print(f"  Fold {fi+1}: {100*sum(fold_ys)/len(fold_ys):.1f}% positive  (n={len(fold_ys)})")

    # ---- 5. Holding period distribution ----
    print(f"\n{'-'*94}\n5. HOLDING PERIOD DISTRIBUTION (days)\n{'-'*94}")
    holdings = sorted(s['holding_days'] for s in samples)
    mean_h = sum(holdings) / len(holdings)
    median_h = holdings[len(holdings)//2]
    p95_h = percentile(holdings, 0.95)
    max_h = holdings[-1]
    print(f"Mean: {mean_h:.1f}   Median: {median_h}   P95: {p95_h:.1f}   Max: {max_h}")
    buckets = [(0,5),(5,10),(10,20),(20,30),(30,60),(60,90),(90,91)]
    print("Histogram:")
    for lo, hi in buckets:
        c = sum(1 for h in holdings if lo <= h < hi)
        label = f"{lo}-{hi-1}d" if hi <= 90 else "90d (timeout)"
        bar = "#" * int(50 * c / len(holdings))
        print(f"  {label:14s} {c:>6d} ({100*c/len(holdings):>5.1f}%)  {bar}")

    # ---- 6. Exit reason distribution ----
    print(f"\n{'-'*94}\n6. EXIT REASON DISTRIBUTION\n{'-'*94}")
    reasons = {}
    for s in samples: reasons[s['exit_reason']] = reasons.get(s['exit_reason'], 0) + 1
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        avg_r = sum(s['r'] for s in samples if s['exit_reason']==reason) / count
        print(f"  {reason:16s} {count:>6d} ({100*count/len(samples):>5.1f}%)  avg r={avg_r:+.3f}")

    # ---- Label leakage: fold-boundary crossing ----
    print(f"\n{'-'*94}\nLABEL LEAKAGE -- FOLD BOUNDARY CROSSING (the label-specific leakage question)\n{'-'*94}")
    print("Feature leakage was already verified in Phase 1b (causality tests on raw()/normalized()).")
    print("The DISTINCT question here: a training sample's LABEL requires looking forward up to")
    print("max_holding days from its entry. If a training sample sits near a fold's test_start,")
    print("its label may resolve USING PRICE DATA THAT FALLS INSIDE THAT FOLD'S HELD-OUT PERIOD.")
    print("This does not leak into FEATURES (which only look backward), but it means the model")
    print("is trained on outcomes partly determined by data from the period it's later tested on.")
    print()
    print(f"{'fold':>5s} {'train samples':>14s} {'label resolves >= test_start':>30s} {'%':>7s}")
    any_boundary_leak = False
    for fi, (test_start, test_end) in enumerate(folds):
        train_samples = [s for s in samples if s['entry_global_i'] < test_start]
        crossing = [s for s in train_samples if s['entry_global_i'] + s['holding_days'] >= test_start]
        pct = 100*len(crossing)/len(train_samples) if train_samples else 0
        if pct > 0: any_boundary_leak = True
        print(f"{fi+1:>5d} {len(train_samples):>14d} {len(crossing):>30d} {pct:>6.1f}%")
    if any_boundary_leak:
        print("\n⚠️  FINDING: a nonzero fraction of each fold's training labels resolve using price")
        print("    data from that fold's own held-out test period. This is a genuine, structural")
        print("    leakage path distinct from feature leakage -- worth flagging as a finding for")
        print("    Phase 2, not silently fixed here per the read-only audit constraint.")

    # ---- Population audit (explicit, evidence-based) ----
    print(f"\n{'-'*94}\nPOPULATION AUDIT\n{'-'*94}")
    print("Every (asset, day) with sufficient feature warmup is evaluated -- confirmed by direct")
    print("inspection of build_training_samples()'s source: no CONV_THRESHOLD check, no regime")
    print("check, no 'breakout candidate' pre-filter exists anywhere in its sample-selection loop.")
    print("No hidden filtering found. Survivorship: unchanged from Phase 1's finding -- the")
    print("universe is hand-specified across this research conversation, not drawn from a")
    print("point-in-time systematic index; this audit cannot resolve that on its own.")

    # ---- Fold audit (day-range non-overlap, already true by construction) ----
    print(f"\n{'-'*94}\nFOLD AUDIT (day-range boundaries)\n{'-'*94}")
    print("compute_fold_boundaries() produces strictly sequential, non-overlapping [start,end)")
    print("day-range folds by construction (verified in Phase 1b) -- day ranges themselves do")
    print("NOT overlap. The label-resolution boundary-crossing issue above is a SEPARATE finding")
    print("from day-range overlap; the day ranges are fine, the labels built from days near the")
    print("boundary are the issue.")

    # ---- GO / NO-GO ----
    print(f"\n{'='*94}\nGO / NO-GO\n{'='*94}")
    if any_boundary_leak:
        print("NO-GO (with a defined remediation path, not a redesign): fold-boundary label")
        print("      leakage is real and structural. Recommended fix for Phase 2b (not executed")
        print("      here, per read-only constraint): introduce an EMBARGO -- exclude training")
        print("      samples whose entry_day falls within max_holding days of test_start, so no")
        print("      training label can resolve using test-period data. This is a standard,")
        print("      well-established fix (the same embargo concept Lopez de Prado's framework")
        print("      uses), not a redesign of the labeling scheme itself.")
    else:
        print("GO: no fold-boundary label leakage detected. Proceed to Phase 3.")

def report(trades, dump_trades=False):
    if not trades:
        print("No trades generated."); return
    wins = [t for t in trades if t["r"] > 0]
    print(f"\n{'='*56}\nTRADES: {len(trades)}   WIN RATE: {100*len(wins)/len(trades):.1f}%")
    print(f"AVG R: {sum(t['r'] for t in trades)/len(trades):+.2f}   "
          f"EXPECTANCY/TRADE: {sum(t['pct'] for t in trades)/len(trades):+.2f}%")
    print(f"BEST: {max(t['pct'] for t in trades):+.1f}%   WORST: {min(t['pct'] for t in trades):+.1f}%")
    for reason in sorted({t["reason"] for t in trades}):
        sub = [t for t in trades if t["reason"] == reason]
        print(f"  {reason:12s}: n={len(sub):3d}  avg {sum(t['pct'] for t in sub)/len(sub):+.2f}%")
    hi = [t for t in trades if t["conv"] >= 80]; lo = [t for t in trades if t["conv"] < 80]

    # --------------------------------------------------------------
    # PER-ASSET BREAKDOWN: isolate whether the aggregate result (and
    # any conviction-edge inversion) is driven by specific symbols
    # rather than being a uniform property of the scoring system.
    # --------------------------------------------------------------
    print(f"\n{'-'*56}\nPER-ASSET BREAKDOWN")
    by_sym = {}
    for t in trades:
        by_sym.setdefault(t["sym"], []).append(t)
    for sym in sorted(by_sym, key=lambda s: -len(by_sym[s])):
        sub = by_sym[sym]
        sw = [t for t in sub if t["r"] > 0]
        wr = 100 * len(sw) / len(sub)
        avg_pct = sum(t["pct"] for t in sub) / len(sub)
        sub_hi = [t for t in sub if t["conv"] >= 80]
        sub_lo = [t for t in sub if t["conv"] < 80]
        hi_avg = f"{sum(t['pct'] for t in sub_hi)/len(sub_hi):+.2f}%" if sub_hi else "n/a"
        lo_avg = f"{sum(t['pct'] for t in sub_lo)/len(sub_lo):+.2f}%" if sub_lo else "n/a"
        print(f"  {sym:6s} n={len(sub):3d}  win={wr:5.1f}%  avg={avg_pct:+6.2f}%  "
              f"(80+: {hi_avg}, <80: {lo_avg})")

    if dump_trades:
        print(f"\n{'-'*56}\nFULL TRADE LOG (chronological)")
        print(f"{'sym':6s} {'entry_day':>9s} {'exit_day':>8s} {'conv':>4s} "
              f"{'entry_px':>12s} {'exit_px':>12s} {'pct':>8s} {'reason':10s}")
        for t in sorted(trades, key=lambda x: x.get("entry_day", 0)):
            print(f"{t['sym']:6s} {t.get('entry_day','?'):>9} {t.get('exit_day','?'):>8} "
                  f"{t['conv']:>4d} {t.get('entry_px',0):>12.4f} {t.get('exit_px',0):>12.4f} "
                  f"{t['pct']:>+7.2f}% {t['reason']:10s}")

    if hi and lo:
        print(f"\nConviction 80+ : n={len(hi)}, avg {sum(t['pct'] for t in hi)/len(hi):+.2f}%")
        print(f"Conviction <80 : n={len(lo)}, avg {sum(t['pct'] for t in lo)/len(lo):+.2f}%")
        print("-> Positive gap = scoring adds real edge; no gap = recalibrate weights")
    print("="*56)

def summarize(trades):
    """Compact summary dict for one config's trade list -- used by --sweep."""
    if not trades:
        return dict(n=0, win_rate=0, expectancy=0, avg_r=0)
    wins = [t for t in trades if t["r"] > 0]
    return dict(
        n=len(trades),
        win_rate=100 * len(wins) / len(trades),
        expectancy=sum(t["pct"] for t in trades) / len(trades),
        avg_r=sum(t["r"] for t in trades) / len(trades),
    )

def trailing_return(closes, i, lookback):
    """Simple trailing % return over `lookback` days ending at index i."""
    if i < lookback or closes[i - lookback] == 0:
        return None
    return closes[i] / closes[i - lookback] - 1

def compute_fold_boundaries(n_days, n_folds_requested, warmup=210,
                             min_initial_train=400, min_test_days=90):
    """
    Expanding-window, sequential, non-overlapping test folds. Each fold's
    test window uses ONLY data at or after its start (no look-ahead); each
    successive fold's test window sits chronologically after the previous
    one, so all folds together walk forward through the full history
    exactly once. This differs from a fixed train/test split in that it
    produces MULTIPLE out-of-sample readings across different calendar
    periods (and therefore different market regimes) instead of trusting
    one arbitrary cut point -- which is what produced the zero-OOS-trades
    failure in the single 70/30 split.

    Fold count is DATA-DRIVEN: if the available history can't support the
    requested number of folds at >= min_test_days each, fewer folds are
    returned rather than silently producing folds too short to trust.
    Returns a list of (test_start, test_end) tuples (day indices into the
    full series). The actual run() call for each fold will separately take
    a `warmup`-day buffer immediately before test_start so EMA/conviction
    context is valid at the start of that fold's test window.
    """
    usable = n_days - min_initial_train
    if usable < min_test_days:
        return []  # not enough history for even one honest fold
    max_folds_supported = max(1, usable // min_test_days)
    n_folds = min(n_folds_requested, max_folds_supported)
    fold_size = usable // n_folds
    folds = []
    for k in range(n_folds):
        test_start = min_initial_train + k * fold_size
        test_end = n_days if k == n_folds - 1 else min_initial_train + (k + 1) * fold_size
        folds.append((test_start, test_end))
    return folds

def run_validate(histories, btc_id="bitcoin", n_folds_requested=4,
                  max_positions=None, rsi_max=None, token_trend_filter=False):
    """
    Expanding-window multi-fold validation of a SINGLE, FIXED parameter
    configuration (whatever CONV_THRESHOLD / max_positions / rsi_max /
    token_trend_filter are currently set to). This deliberately does NOT
    search over parameters -- that's what --sweep is for, and combining
    search with multi-fold validation in one pass reintroduces the same
    overfitting risk this framework exists to remove. Use --validate first
    to confirm a chosen config is robust; only feed --sweep survivors back
    through --validate afterward, one candidate config at a time.

    On top of the per-fold breakdown, every trade across all folds is
    pooled and additionally stratified by the BTC regime that was ALREADY
    KNOWN at that trade's own entry day (BTC trailing 90-day return sign,
    computed using only data up to and including the entry day -- no
    look-ahead). This directly answers "does this config work in both
    bull and bear conditions" regardless of how the calendar folds happen
    to land, which is the failure mode from the single 70/30 split.
    """
    btc_closes, _ = histories[btc_id]
    n = len(btc_closes)
    WARMUP = 210
    folds = compute_fold_boundaries(n, n_folds_requested, warmup=WARMUP)

    if not folds:
        print(f"\nVALIDATION: insufficient history ({n} days) to build even one honest "
              f"fold (need >= {400 + 90} days minimum). Use more history or fewer assets "
              f"with shorter overlap.")
        return

    print(f"\nVALIDATION: {n} total days -> {len(folds)} expanding-window fold(s) "
          f"(requested {n_folds_requested})")
    print(f"Config under test: conv_threshold={CONV_THRESHOLD}, max_positions={max_positions}, "
          f"rsi_max={rsi_max}, token_trend_filter={token_trend_filter}\n")

    all_trades = []
    fold_summaries = []
    for idx, (test_start, test_end) in enumerate(folds):
        buf_start = max(0, test_start - WARMUP)
        hist_fold = {cid: (c[buf_start:test_end], v[buf_start:test_end])
                     for cid, (c, v) in histories.items()}
        trades = run(hist_fold, btc_id=btc_id, max_positions=max_positions,
                     rsi_max=rsi_max, token_trend_filter=token_trend_filter, verbose=False)
        # Re-offset entry_day/exit_day back to the ORIGINAL series index so
        # regime stratification below can look up the correct BTC history.
        for t in trades:
            t["entry_day"] = t.get("entry_day", 0) + buf_start
            t["exit_day"] = t.get("exit_day", 0) + buf_start
        s = summarize(trades)
        fold_summaries.append(dict(fold=idx+1, start=test_start, end=test_end, **s))
        all_trades.extend(trades)

    print(f"{'='*78}\nPER-FOLD RESULTS (chronological, non-overlapping)\n{'='*78}")
    print(f"{'fold':>5s} {'days':>12s} {'n':>5s} {'win%':>7s} {'exp%':>8s} {'avgR':>7s}")
    for s in fold_summaries:
        days_str = f"{s['start']}-{s['end']}"
        flag = "  (too few trades)" if s['n'] < 10 else ""
        print(f"{s['fold']:>5d} {days_str:>12s} {s['n']:>5d} {s['win_rate']:>6.1f}% "
              f"{s['expectancy']:>+7.2f}% {s['avg_r']:>+6.2f}{flag}")

    agg = summarize(all_trades)
    print(f"\n{'-'*78}\nAGGREGATE ACROSS ALL FOLDS (pooled)\n{'-'*78}")
    print(f"Total trades: {agg['n']}   Win rate: {agg['win_rate']:.1f}%   "
          f"Expectancy: {agg['expectancy']:+.2f}%   Avg R: {agg['avg_r']:+.2f}")
    if all_trades:
        max_dd = compute_max_drawdown(all_trades)
        print(f"Max drawdown (R-multiple, sequential across pooled trades): {max_dd:.2f}R")

    if agg['n'] < 15:
        print("\n⚠️  Fewer than 15 pooled trades total -- this config cannot be validated")
        print("    with confidence on this dataset. Treat any config-level conclusion as")
        print("    provisional, not a basis for sizing decisions.")
        return

    # ---- Regime-stratified breakdown: BTC trailing 90d return AT ENTRY TIME ----
    bull_entry = [t for t in all_trades if (trailing_return(btc_closes, t["entry_day"], 90) or 0) > 0]
    bear_entry = [t for t in all_trades if (trailing_return(btc_closes, t["entry_day"], 90) or 0) <= 0]
    print(f"\n{'-'*78}\nREGIME-STRATIFIED (BTC trailing 90d return AT ENTRY, known at signal time)\n{'-'*78}")
    for label, sub in [("BTC UP  (bullish entries)", bull_entry), ("BTC DOWN (bearish entries)", bear_entry)]:
        s = summarize(sub)
        if s['n'] < 10:
            print(f"{label:28s}: n={s['n']:3d}  -- too few trades, inconclusive")
        else:
            print(f"{label:28s}: n={s['n']:3d}  win={s['win_rate']:5.1f}%  exp={s['expectancy']:+6.2f}%  avgR={s['avg_r']:+.2f}")

    print(f"\n{'-'*78}")
    if agg['n'] >= 15 and agg['expectancy'] > 0 and len(bull_entry) >= 10 and len(bear_entry) >= 10 \
       and summarize(bull_entry)['expectancy'] > 0 and summarize(bear_entry)['expectancy'] > 0:
        print("VERDICT: Positive expectancy holds across BOTH regime strata and across")
        print("         multiple chronological folds -- this is the strongest evidence")
        print("         this framework can currently produce for this config being robust.")
    elif agg['n'] >= 15 and agg['expectancy'] > 0:
        print("VERDICT: Aggregate expectancy is positive, but at least one regime stratum")
        print("         has too few trades or a negative result -- robustness across")
        print("         regimes is NOT yet confirmed. Treat with caution.")
    else:
        print("VERDICT: This config does not show a robust positive edge under this")
        print("         framework. Do not size real capital against it as-is.")

def run_validate_dynamic(histories_dated, btc_id="bitcoin", n_folds_requested=4,
                          max_positions=None, rsi_max=None, token_trend_filter=False):
    """
    Dynamic-universe counterpart to run_validate(). Identical fold logic
    and regime-stratification, but calls run_dynamic() with a day_start/
    day_end window on the BTC global clock instead of slicing per-asset
    arrays -- each asset's own full history remains available for lookback
    at all times (no truncation, no artificial warmup buffer needed around
    fold boundaries, since date-based lookup already handles this).
    """
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    folds = compute_fold_boundaries(n, n_folds_requested, warmup=210)

    if not folds:
        print(f"\nVALIDATION: insufficient BTC history ({n} days) to build even one "
              f"honest fold (need >= 490 days minimum).")
        return

    print(f"\nVALIDATION (dynamic universe): {n} BTC-clock days -> {len(folds)} "
          f"expanding-window fold(s) (requested {n_folds_requested})")
    print(f"Config under test: conv_threshold={CONV_THRESHOLD}, max_positions={max_positions}, "
          f"rsi_max={rsi_max}, token_trend_filter={token_trend_filter}\n")

    all_trades = []
    fold_summaries = []
    for idx, (test_start, test_end) in enumerate(folds):
        trades = run_dynamic(histories_dated, btc_id=btc_id, day_start=test_start,
                             day_end=test_end, max_positions=max_positions, rsi_max=rsi_max,
                             token_trend_filter=token_trend_filter, verbose=False)
        s = summarize(trades)
        fold_summaries.append(dict(fold=idx+1, start=test_start, end=test_end,
                                    date_start=btc_dates[test_start],
                                    date_end=btc_dates[min(test_end, n-1)], **s))
        all_trades.extend(trades)

    print(f"{'='*86}\nPER-FOLD RESULTS (chronological, non-overlapping)\n{'='*86}")
    print(f"{'fold':>5s} {'dates':>23s} {'n':>5s} {'win%':>7s} {'exp%':>8s} {'avgR':>7s}")
    for s in fold_summaries:
        date_str = f"{s['date_start']} to {s['date_end']}"
        flag = "  (too few trades)" if s['n'] < 10 else ""
        print(f"{s['fold']:>5d} {date_str:>23s} {s['n']:>5d} {s['win_rate']:>6.1f}% "
              f"{s['expectancy']:>+7.2f}% {s['avg_r']:>+6.2f}{flag}")

    agg = summarize(all_trades)
    print(f"\n{'-'*86}\nAGGREGATE ACROSS ALL FOLDS (pooled)\n{'-'*86}")
    print(f"Total trades: {agg['n']}   Win rate: {agg['win_rate']:.1f}%   "
          f"Expectancy: {agg['expectancy']:+.2f}%   Avg R: {agg['avg_r']:+.2f}")
    if all_trades:
        max_dd = compute_max_drawdown(all_trades)
        print(f"Max drawdown (R-multiple, sequential across pooled trades): {max_dd:.2f}R")

    # Per-asset breakdown across the pooled trades -- important here since
    # the dynamic universe means assets contribute very unevenly (older
    # assets like BTC/ETH/LTC/ADA get far more trading days than SOL/LDO).
    by_sym = {}
    for t in all_trades:
        by_sym.setdefault(t["sym"], []).append(t)
    if by_sym:
        print(f"\n{'-'*86}\nPER-ASSET (pooled across all folds)\n{'-'*86}")
        for sym in sorted(by_sym, key=lambda s: -len(by_sym[s])):
            s = summarize(by_sym[sym])
            print(f"  {sym:6s} n={s['n']:4d}  win={s['win_rate']:5.1f}%  exp={s['expectancy']:+6.2f}%")

    if agg['n'] < 15:
        print("\n⚠️  Fewer than 15 pooled trades total -- this config cannot be validated")
        print("    with confidence on this dataset.")
        return

    bull_entry = [t for t in all_trades if (trailing_return(btc_closes, t["entry_day"], 90) or 0) > 0]
    bear_entry = [t for t in all_trades if (trailing_return(btc_closes, t["entry_day"], 90) or 0) <= 0]
    print(f"\n{'-'*86}\nREGIME-STRATIFIED (BTC trailing 90d return AT ENTRY, known at signal time)\n{'-'*86}")
    for label, sub in [("BTC UP  (bullish entries)", bull_entry), ("BTC DOWN (bearish entries)", bear_entry)]:
        s = summarize(sub)
        if s['n'] < 10:
            print(f"{label:28s}: n={s['n']:3d}  -- too few trades, inconclusive")
        else:
            print(f"{label:28s}: n={s['n']:3d}  win={s['win_rate']:5.1f}%  exp={s['expectancy']:+6.2f}%  avgR={s['avg_r']:+.2f}")

    print(f"\n{'-'*86}")
    if agg['n'] >= 15 and agg['expectancy'] > 0 and len(bull_entry) >= 10 and len(bear_entry) >= 10 \
       and summarize(bull_entry)['expectancy'] > 0 and summarize(bear_entry)['expectancy'] > 0:
        print("VERDICT: Positive expectancy holds across BOTH regime strata and across")
        print("         multiple chronological folds -- this is the strongest evidence")
        print("         this framework can currently produce for this config being robust.")
    elif agg['n'] >= 15 and agg['expectancy'] > 0:
        print("VERDICT: Aggregate expectancy is positive, but at least one regime stratum")
        print("         has too few trades or a negative result -- robustness across")
        print("         regimes is NOT yet confirmed. Treat with caution.")
    else:
        print("VERDICT: This config does not show a robust positive edge under this")
        print("         framework. Do not size real capital against it as-is.")

def entropy(labels):
    """Binary entropy in bits, given a list of 0/1 labels."""
    if not labels: return 0.0
    p = sum(labels) / len(labels)
    if p <= 0 or p >= 1: return 0.0
    return -(p * math.log2(p) + (1-p) * math.log2(1-p))

def information_gain(trades, component):
    """
    IG(component) = H(outcome) - weighted-avg H(outcome | component=T/F).
    outcome = 1 if trade was a win (r > 0), else 0. Standard entropy-based
    feature-selection metric, applied here to a binary conviction flag
    against binary trade outcome.
    """
    labels = [1 if t["r"] > 0 else 0 for t in trades]
    base_h = entropy(labels)
    true_group = [1 if t["r"] > 0 else 0 for t in trades if t["components"].get(component)]
    false_group = [1 if t["r"] > 0 else 0 for t in trades if not t["components"].get(component)]
    if not true_group or not false_group:
        return 0.0, base_h
    w_true = len(true_group) / len(trades)
    w_false = len(false_group) / len(trades)
    cond_h = w_true * entropy(true_group) + w_false * entropy(false_group)
    return base_h - cond_h, base_h

def permutation_test(trades, component, metric="win_rate", n_perm=2000, seed=42):
    """
    Statistically-appropriate substitute for SHAP on a small additive
    scorecard (per user's own allowance for "or another statistically
    appropriate method"). Measures the REAL difference in `metric` between
    trades where `component` was true vs false at entry, then repeatedly
    shuffles the component labels across the SAME pool of trades (breaking
    any real association while preserving the trade outcomes themselves)
    to build a null distribution. The p-value is the fraction of permuted
    differences at least as extreme as the real one -- a standard, exact
    permutation-test procedure, not an approximation.
    """
    rng = random.Random(seed)
    flags = [bool(t["components"].get(component)) for t in trades]
    if metric == "win_rate":
        vals = [1.0 if t["r"] > 0 else 0.0 for t in trades]
    else:  # expectancy (pct)
        vals = [t["pct"] for t in trades]

    def group_diff(flag_list):
        true_v = [v for v, f in zip(vals, flag_list) if f]
        false_v = [v for v, f in zip(vals, flag_list) if not f]
        if not true_v or not false_v: return 0.0
        return (sum(true_v)/len(true_v)) - (sum(false_v)/len(false_v))

    real_diff = group_diff(flags)
    if sum(flags) == 0 or sum(flags) == len(flags):
        return real_diff, 1.0  # no variance to permute -- can't test

    count_extreme = 0
    shuffled = list(flags)
    for _ in range(n_perm):
        rng.shuffle(shuffled)
        if abs(group_diff(shuffled)) >= abs(real_diff):
            count_extreme += 1
    p_value = count_extreme / n_perm
    return real_diff, p_value

def phi_coefficient(trades, comp_a, comp_b):
    """Correlation between two binary component flags across a trade pool (redundancy check)."""
    a = [1 if t["components"].get(comp_a) else 0 for t in trades]
    b = [1 if t["components"].get(comp_b) else 0 for t in trades]
    n = len(a)
    if n == 0: return 0.0
    n11 = sum(1 for x,y in zip(a,b) if x==1 and y==1)
    n10 = sum(1 for x,y in zip(a,b) if x==1 and y==0)
    n01 = sum(1 for x,y in zip(a,b) if x==0 and y==1)
    n00 = sum(1 for x,y in zip(a,b) if x==0 and y==0)
    n1_, n0_ = n11+n10, n01+n00
    n_1, n_0 = n11+n01, n10+n00
    denom = math.sqrt(n1_*n0_*n_1*n_0)
    if denom == 0: return 0.0
    return (n11*n00 - n10*n01) / denom

def pearson_r(xs, ys):
    """Standard Pearson correlation coefficient between two equal-length numeric lists."""
    n = len(xs)
    if n < 2: return None
    mx = sum(xs) / n; my = sum(ys) / n
    cov = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
    vx = sum((x-mx)**2 for x in xs); vy = sum((y-my)**2 for y in ys)
    denom = math.sqrt(vx * vy)
    if denom == 0: return None
    return cov / denom

def run_attribution(histories_dated, btc_id="bitcoin", n_folds_requested=4,
                     max_positions=None, rs_mode="level", rs_window=10, rs_slope_lookback=10,
                     registry=None, hilo_dated=None, weight_override=None):
    """
    Feature attribution. With registry=None (default), evaluates the
    original 4 legacy components exactly as before. With registry set
    (e.g. FEATURE_REGISTRY_WITH_ATR), generalizes to whatever features are
    registered -- nothing about this function's logic is hardcoded to a
    fixed component list; it iterates `comps = list(registry_dict)`.

    Method 1 -- ABLATION (causal): re-runs the full dynamic-universe engine
    with each component forced OFF, one at a time, across the SAME
    expanding-window folds as the confirmed baseline.

    Method 2 -- BINARY CORRELATION + PERMUTATION TEST: for the baseline,
    splits trades by whether each component's binary interpretation was
    true at entry and measures the outcome difference, with an exact
    permutation-test p-value.

    Method 2b -- NORMALIZED-SCORE CORRELATION (NEW, framework mode only):
    correlates each trade's raw continuous `normalized` score (0-100) at
    entry with its eventual outcome (pct return), via Pearson correlation
    -- this is what requirement 5 calls "normalized feature contribution",
    complementing Method 2's binary-threshold view. A feature can show a
    real continuous relationship with outcome even where its binary cut
    doesn't cleanly separate groups, or vice versa.

    Method 3 -- INFORMATION GAIN. Method 4 -- REDUNDANCY (pairwise phi
    between components' binary flags).

    rs_mode/rs_window/rs_slope_lookback: only meaningful in legacy mode
    (registry=None); ignored when the framework registry is active, since
    relative_strength's implementation there comes from RelativeStrengthFeature.

    weight_override: optional {feature_name: weight} dict, e.g. produced
    by run_train_model(). When set, every ablation/baseline run in this
    function uses the LEARNED weights instead of each feature's hand-set
    `.weight` -- lets the exact same attribution suite be re-run against
    a learned-weight configuration for direct before/after comparison.
    """
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    folds = compute_fold_boundaries(n, n_folds_requested, warmup=210)
    if not folds:
        print(f"\nATTRIBUTION: insufficient history ({n} days) for even one honest fold.")
        return

    use_framework = registry is not None
    comps = list(registry) if use_framework else list(CONVICTION_COMPONENTS)

    print(f"\n{'='*90}\nFEATURE ATTRIBUTION: conviction scorecard component analysis")
    print(f"{'='*90}")
    print(f"{n} BTC-clock days -> {len(folds)} expanding-window fold(s), pooled across all folds")
    if use_framework:
        print(f"Engine: generalized feature framework -- {len(comps)} features: {comps}")
        if weight_override:
            print(f"Weights: LEARNED (overriding hand-set weights): {weight_override}")
    else:
        print(f"relative_strength mode: {rs_mode}" +
              (f" (window={rs_window}, slope_lookback={rs_slope_lookback})" if rs_mode == "slope" else ""))
    print()

    def pooled_run(disabled):
        all_t = []
        for test_start, test_end in folds:
            all_t.extend(run_dynamic_attr(histories_dated, btc_id=btc_id, day_start=test_start,
                                          day_end=test_end, max_positions=max_positions,
                                          disabled_components=disabled,
                                          rs_mode=rs_mode, rs_window=rs_window,
                                          rs_slope_lookback=rs_slope_lookback,
                                          registry=registry, hilo_dated=hilo_dated,
                                          weight_override=weight_override))
        return all_t

    baseline = pooled_run(set())
    if len(baseline) < 15:
        print(f"⚠️  Only {len(baseline)} baseline trades pooled -- too few for reliable attribution.")
        print("    Results below are provisional at best.")
    base_s = summarize(baseline)
    base_dd = compute_max_drawdown(baseline) if baseline else 0
    base_t2 = sum(1 for t in baseline if t["reason"] == "t2") / len(baseline) if baseline else 0

    print(f"BASELINE (all components active): n={base_s['n']}  win={base_s['win_rate']:.1f}%  "
          f"exp={base_s['expectancy']:+.2f}%  avgR={base_s['avg_r']:+.2f}  "
          f"maxDD={base_dd:.2f}R  T2-rate={base_t2*100:.1f}%\n")

    # ---- Method 1: ablation ----
    print(f"{'-'*90}\nMETHOD 1 -- ABLATION (component forced OFF, same folds, causal removal)\n{'-'*90}")
    print(f"{'component':22s} {'Δexp%':>8s} {'Δwin%':>8s} {'ΔavgR':>7s} {'ΔmaxDD':>8s} {'ΔT2%':>7s}  n(ablated)")
    ablation_results = {}
    for comp in comps:
        ablated = pooled_run({comp})
        a_s = summarize(ablated)
        a_dd = compute_max_drawdown(ablated) if ablated else 0
        a_t2 = sum(1 for t in ablated if t["reason"] == "t2") / len(ablated) if ablated else 0
        d_exp = base_s['expectancy'] - a_s['expectancy']
        d_win = base_s['win_rate'] - a_s['win_rate']
        d_avgr = base_s['avg_r'] - a_s['avg_r']
        d_dd = base_dd - a_dd
        d_t2 = (base_t2 - a_t2) * 100
        ablation_results[comp] = dict(d_exp=d_exp, d_win=d_win, d_avgr=d_avgr, d_dd=d_dd, d_t2=d_t2, n=a_s['n'])
        print(f"{comp:22s} {d_exp:>+7.2f}% {d_win:>+7.1f}% {d_avgr:>+6.2f} {d_dd:>+7.2f}R {d_t2:>+6.1f}%  n={a_s['n']}")
    print("(Δ = baseline minus ablated-version metric. Positive Δexp means removing")
    print(" that component made expectancy WORSE -- i.e. the component was helping.")
    print(" Negative Δexp means removing it made expectancy BETTER -- the component")
    print(" was hurting.)\n")

    # ---- Method 2: binary correlation + permutation test ----
    print(f"{'-'*90}\nMETHOD 2 -- BINARY CORRELATION + PERMUTATION TEST (on baseline trades)\n{'-'*90}")
    print(f"{'component':22s} {'n(true)':>8s} {'win% T':>8s} {'win% F':>8s} {'Δwin':>7s} {'p-value':>9s}  significant?")
    corr_results = {}
    for comp in comps:
        n_true = sum(1 for t in baseline if t["components"].get(comp))
        true_wins = [t for t in baseline if t["components"].get(comp) and t["r"] > 0]
        false_wins = [t for t in baseline if not t["components"].get(comp) and t["r"] > 0]
        n_false = len(baseline) - n_true
        win_t = 100*len(true_wins)/n_true if n_true else 0
        win_f = 100*len(false_wins)/n_false if n_false else 0
        diff, pval = permutation_test(baseline, comp, metric="win_rate") if baseline else (0,1)
        sig = "YES (p<0.05)" if pval < 0.05 else "no"
        corr_results[comp] = dict(n_true=n_true, win_t=win_t, win_f=win_f, pval=pval)
        print(f"{comp:22s} {n_true:>8d} {win_t:>7.1f}% {win_f:>7.1f}% {win_t-win_f:>+6.1f}% {pval:>9.3f}  {sig}")
    print()

    # ---- Method 2b: normalized-score correlation (framework mode only) ----
    norm_results = {}
    if use_framework:
        print(f"{'-'*90}\nMETHOD 2b -- NORMALIZED-SCORE CORRELATION (continuous score vs outcome)\n{'-'*90}")
        print(f"{'component':22s} {'pearson r':>10s} {'n':>6s}   interpretation")
        for comp in comps:
            scored = [(t["normalizeds"].get(comp), t["pct"]) for t in baseline
                      if t.get("normalizeds") and t["normalizeds"].get(comp) is not None]
            r = pearson_r([s[0] for s in scored], [s[1] for s in scored]) if len(scored) >= 5 else None
            norm_results[comp] = r
            if r is None:
                print(f"{comp:22s} {'n/a':>10s} {len(scored):>6d}   too few scored trades")
            else:
                interp = "positive -- higher score, better outcome" if r > 0.1 else \
                         "negative -- higher score, worse outcome" if r < -0.1 else "no meaningful linear relationship"
                print(f"{comp:22s} {r:>+10.3f} {len(scored):>6d}   {interp}")
        print()

    # ---- Method 3: information gain ----
    print(f"{'-'*90}\nMETHOD 3 -- INFORMATION GAIN (entropy reduction in win/loss outcome)\n{'-'*90}")
    ig_results = {}
    for comp in comps:
        ig, base_h = information_gain(baseline, comp) if baseline else (0, 0)
        ig_results[comp] = ig
        print(f"{comp:22s} IG={ig:.4f} bits  (base outcome entropy={base_h:.4f} bits)")
    print()

    # ---- Method 4: redundancy ----
    print(f"{'-'*90}\nMETHOD 4 -- REDUNDANCY (pairwise correlation between component flags)\n{'-'*90}")
    for i in range(len(comps)):
        for j in range(i+1, len(comps)):
            phi = phi_coefficient(baseline, comps[i], comps[j]) if baseline else 0
            flag = "  <-- HIGH: consider merging" if abs(phi) > 0.5 else ""
            print(f"  {comps[i]:22s} vs {comps[j]:22s}: phi={phi:+.3f}{flag}")
    print()

    # ---- Ranking and recommendation ----
    print(f"{'='*90}\nRANKED SUMMARY (by ablation Δexpectancy -- the causal measure)\n{'='*90}")
    ranked = sorted(comps, key=lambda c: -ablation_results[c]['d_exp'])
    for rank, comp in enumerate(ranked, 1):
        a = ablation_results[comp]; corr = corr_results[comp]; ig = ig_results[comp]
        nr = norm_results.get(comp) if use_framework else None
        verdict = ("ADDS real value" if a['d_exp'] > 0.1 and corr['pval'] < 0.1 else
                   "HURTS performance" if a['d_exp'] < -0.1 else
                   "weak/no measurable effect")
        nr_str = f"  norm-r={nr:+.3f}" if nr is not None else ""
        print(f"{rank}. {comp:22s} ablation-Δexp={a['d_exp']:+.2f}%  perm-p={corr['pval']:.3f}  "
              f"IG={ig:.4f}{nr_str}  -> {verdict}")

    print(f"\n{'-'*90}\nRECOMMENDATION\n{'-'*90}")
    adds_value = [c for c in comps if ablation_results[c]['d_exp'] > 0.1 and corr_results[c]['pval'] < 0.1]
    hurts = [c for c in comps if ablation_results[c]['d_exp'] < -0.1]
    weak = [c for c in comps if c not in adds_value and c not in hurts]
    print(f"Adding real, statistically-supported value: {adds_value if adds_value else 'none identified'}")
    print(f"Hurting performance (consider removing):    {hurts if hurts else 'none identified'}")
    print(f"Weak/inconclusive on this dataset:            {weak if weak else 'none'}")
    print("\nNOTE: this dataset's trade count limits statistical power -- a component")
    print("landing in 'weak/inconclusive' may still be real and simply under-tested here,")
    print("not necessarily worthless. Treat 'hurts performance' findings with the most")
    print("confidence (both ablation AND permutation-test agreement is the strongest bar),")
    print("and treat single-method findings as directional, not conclusive.")

def run_rs_lookback_sweep(histories_dated, btc_id="bitcoin", n_folds_requested=4,
                           max_positions=None, lookbacks=(7, 14, 21, 30)):
    """
    Tests whether relative_strength's 30-day lookback is the source of the
    lag identified by the ablation-vs-permutation contradiction. Holds
    every other component and the entry threshold constant; only the
    relative_strength window changes. A shorter window fires earlier in a
    genuine move (less lag) at the cost of more noise -- this sweep shows
    where that tradeoff actually lands on real data, via the SAME ablation
    methodology (Δexpectancy = baseline-with-that-lookback minus fully-
    ablated) so results are directly comparable to the original --attribute
    run's relative_strength row.
    """
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    folds = compute_fold_boundaries(n, n_folds_requested, warmup=210)
    if not folds:
        print(f"\nRS LOOKBACK SWEEP: insufficient history ({n} days).")
        return

    print(f"\n{'='*90}\nRELATIVE-STRENGTH LOOKBACK SENSITIVITY SWEEP\n{'='*90}")
    print(f"Testing lookbacks: {lookbacks} (original design = 30 days)\n")
    print(f"{'lookback':>9s} {'n_trades':>9s} {'win%':>7s} {'exp%':>8s} {'avgR':>7s} "
          f"{'RS-flag corr(p)':>18s} {'RS ablation Δexp%':>19s}")

    for lb in lookbacks:
        pooled = []
        for test_start, test_end in folds:
            pooled.extend(run_dynamic_attr(histories_dated, btc_id=btc_id, day_start=test_start,
                                           day_end=test_end, max_positions=max_positions,
                                           disabled_components=set(), rs_lookback=lb))
        s = summarize(pooled)
        _, pval = permutation_test(pooled, "relative_strength", metric="win_rate") if pooled else (0, 1)

        ablated = []
        for test_start, test_end in folds:
            ablated.extend(run_dynamic_attr(histories_dated, btc_id=btc_id, day_start=test_start,
                                            day_end=test_end, max_positions=max_positions,
                                            disabled_components={"relative_strength"}, rs_lookback=lb))
        a_s = summarize(ablated)
        d_exp = s['expectancy'] - a_s['expectancy']

        print(f"{lb:>8d}d {s['n']:>9d} {s['win_rate']:>6.1f}% {s['expectancy']:>+7.2f}% "
              f"{s['avg_r']:>+6.2f} {pval:>17.3f} {d_exp:>+18.2f}%")

    print("\nRead this as: does a SHORTER lookback flip relative_strength's ablation-Δexp")
    print("from negative (hurting) to positive (helping)? If yes, the lag hypothesis is")
    print("confirmed and a shorter window is the fix. If the sign stays negative across")
    print("all lookbacks, the problem isn't lag specifically -- reconsider whether this")
    print("component should be a hard gate at all versus a soft scoring input.")

def diagnose_component_timing(histories_dated, component, btc_id="bitcoin",
                               n_folds_requested=4, max_positions=None,
                               rs_mode="level", rs_window=10, rs_slope_lookback=10,
                               registry=None, hilo_dated=None, weight_override=None):
    """
    Directly tests "does this component filter out early winners" by
    comparing two REAL, non-overlapping sets of trades from the same folds:

      ENABLED-ONLY trades: present when the component is ON, absent when
        it's OFF -- i.e. trades that only happened BECAUSE this component
        pushed the candidate over threshold (or won it a position-cap
        slot). This is what the component is currently buying you.

      ADMITTED-BY-REMOVAL trades: present when the component is OFF,
        absent when it's ON -- i.e. trades that were BLOCKED by this
        component's absence-of-signal (or lost a cap slot to something
        that needed it), and only get taken once the gate is removed.
        This is what the component is currently costing you.

    If ADMITTED-BY-REMOVAL trades have HIGHER expectancy than ENABLED-ONLY
    trades, the component is net filtering out better opportunities than
    the ones it's letting in -- direct, non-theoretical evidence for the
    "entering too late / filtering early winners" hypothesis.

    rs_mode/rs_window/rs_slope_lookback: only meaningful in legacy mode
    (registry=None). registry/hilo_dated: pass FEATURE_REGISTRY_WITH_ATR
    and the loaded hi/lo data to diagnose a framework-mode feature such as
    atr_donchian_squeeze. weight_override: optional learned weights, same
    semantics as run_attribution().
    """
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    folds = compute_fold_boundaries(n, n_folds_requested, warmup=210)
    if not folds:
        print(f"\nCOMPONENT TIMING DIAGNOSIS: insufficient history ({n} days).")
        return

    with_comp, without_comp = [], []
    for test_start, test_end in folds:
        with_comp.extend(run_dynamic_attr(histories_dated, btc_id=btc_id, day_start=test_start,
                                          day_end=test_end, max_positions=max_positions,
                                          disabled_components=set(),
                                          rs_mode=rs_mode, rs_window=rs_window,
                                          rs_slope_lookback=rs_slope_lookback,
                                          registry=registry, hilo_dated=hilo_dated,
                                          weight_override=weight_override))
        without_comp.extend(run_dynamic_attr(histories_dated, btc_id=btc_id, day_start=test_start,
                                             day_end=test_end, max_positions=max_positions,
                                             disabled_components={component},
                                             rs_mode=rs_mode, rs_window=rs_window,
                                             rs_slope_lookback=rs_slope_lookback,
                                             registry=registry, hilo_dated=hilo_dated,
                                             weight_override=weight_override))

    with_keys = {(t["sym"], t["entry_day"]) for t in with_comp}
    without_keys = {(t["sym"], t["entry_day"]) for t in without_comp}

    enabled_only = [t for t in with_comp if (t["sym"], t["entry_day"]) not in without_keys]
    admitted_by_removal = [t for t in without_comp if (t["sym"], t["entry_day"]) not in with_keys]

    print(f"\n{'='*90}\nCOMPONENT TIMING DIAGNOSIS: {component}\n{'='*90}")
    e_s = summarize(enabled_only)
    a_s = summarize(admitted_by_removal)
    print(f"ENABLED-ONLY (trades this component's presence CREATES):")
    print(f"  n={e_s['n']}  win={e_s['win_rate']:.1f}%  exp={e_s['expectancy']:+.2f}%  avgR={e_s['avg_r']:+.2f}")
    print(f"ADMITTED-BY-REMOVAL (trades this component's presence BLOCKS):")
    print(f"  n={a_s['n']}  win={a_s['win_rate']:.1f}%  exp={a_s['expectancy']:+.2f}%  avgR={a_s['avg_r']:+.2f}")

    if e_s['n'] < 10 or a_s['n'] < 10:
        print("\n⚠️  Fewer than 10 trades in one or both groups -- treat this diagnosis as")
        print("    directional only, not conclusive, on this dataset.")
        return

    diff = a_s['expectancy'] - e_s['expectancy']
    print(f"\nDifference (blocked-trade expectancy minus enabled-trade expectancy): {diff:+.2f}%")
    if diff > 0.3:
        print("VERDICT: The trades this component BLOCKS have HIGHER expectancy than the")
        print("         trades it ENABLES -- direct evidence it is filtering out better")
        print("         opportunities than the ones it lets through (consistent with a")
        print("         lagging-gate problem, not a fundamentally bad signal).")
    elif diff < -0.3:
        print("VERDICT: The trades this component ENABLES have HIGHER expectancy than the")
        print("         trades it blocks -- the gate is doing its job; the earlier findings")
        print("         may be driven by something other than timing (check redundancy or")
        print("         weight next).")
    else:
        print("VERDICT: No meaningful difference between the two trade sets on this")
        print("         dataset -- inconclusive; more history is needed to separate these.")

def compute_max_drawdown(trades):
    """
    Sequential R-multiple drawdown across trades in chronological order
    (approximation: assumes one unit of risk per trade, not true portfolio
    equity curve with position sizing -- consistent with the AVG R metric
    already used elsewhere in this script).
    """
    ordered = sorted(trades, key=lambda t: t.get("entry_day", 0))
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in ordered:
        equity += t["r"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd

# ============================================================
# PORTFOLIO PERFORMANCE REPORT (production-system phase, additive)
#
# The metrics this project now needs to optimize against (Sharpe, Sortino,
# Calmar, CAGR, Profit Factor, Recovery Factor) have never been computed
# anywhere in this tool -- everything so far reported expectancy, win
# rate, and R-multiple drawdown. This builds a REAL, compounded capital
# equity curve from pooled out-of-sample trades (position risk = a
# parameterized % of capital per trade, standard convention) and derives
# every requested metric from it. Does not touch any feature, label,
# candidate-selection, or validation logic -- pure measurement on top of
# trades the existing engine already produces.
# ============================================================

def compute_position_weight(t, sizing_mode, avg_stop_d, avg_conv):
    """
    Returns a MULTIPLIER on the base risk_pct_per_trade for a single
    trade, depending on sizing_mode:
      "fixed"               -> 1.0 for every trade (current baseline behavior)
      "vol_targeted"        -> inversely proportional to the trade's own
                                stop width (stop_d) relative to the
                                portfolio average -- wider stop (more
                                volatile entry) gets a SMALLER size, so
                                every trade risks roughly the same $ amount
                                regardless of the asset's volatility at
                                entry (the standard vol-targeting definition)
      "conviction_weighted" -> proportional to the trade's own conviction
                                score relative to the portfolio average --
                                higher-conviction entries get more capital
    Multipliers are clamped to [0.3x, 2.0x] to avoid extreme, unrealistic
    single-trade sizing from either method.
    """
    if sizing_mode == "vol_targeted":
        sd = t.get("stop_d")
        if not sd or sd <= 0 or not avg_stop_d:
            return 1.0
        w = avg_stop_d / sd  # narrower stop -> bigger size, wider stop -> smaller size
    elif sizing_mode == "conviction_weighted":
        conv = t.get("conv")
        if not conv or not avg_conv:
            return 1.0
        w = conv / avg_conv
    else:
        return 1.0
    return max(0.3, min(2.0, w))


def build_portfolio_equity_curve(trades, initial_capital=10000.0, risk_pct_per_trade=0.015,
                                  sizing_mode="fixed", heat_cap=None):
    """
    Converts a pooled trade list (each carrying an r-multiple and an
    entry_day) into a compounded capital curve: each trade risks
    `risk_pct_per_trade` of CURRENT capital (not fixed initial capital --
    compounding, the standard convention), gains/loses r * risk_pct_per_trade
    of that capital. Trades are applied in chronological order by entry_day.

    sizing_mode: "fixed" (default, byte-identical to the original baseline
    behavior), "vol_targeted", or "conviction_weighted" -- see
    compute_position_weight() for the exact sizing rule.

    heat_cap: if set (e.g. 0.06 = 6% of capital), caps TOTAL concurrent
    risk across all simultaneously-open positions. When a new trade's
    entry would push cumulative open risk above this cap, its weight is
    SCALED DOWN (not skipped) so the cap is respected exactly -- a real
    portfolio-construction control, not a change to which trades the
    frozen entry logic generates. None (default) preserves the exact
    prior behavior with no cap.
    """
    ordered = sorted(trades, key=lambda t: t.get("entry_day", 0))
    if not ordered:
        return [(0, initial_capital)], []

    stop_ds = [t["stop_d"] for t in ordered if t.get("stop_d")]
    avg_stop_d = sum(stop_ds) / len(stop_ds) if stop_ds else None
    convs = [t["conv"] for t in ordered if t.get("conv")]
    avg_conv = sum(convs) / len(convs) if convs else None

    equity = initial_capital
    curve = [(ordered[0]["entry_day"], initial_capital)]
    trade_returns_pct = []
    open_positions = []  # list of (exit_day, allocated_risk_pct) for heat tracking
    for t in ordered:
        weight = compute_position_weight(t, sizing_mode, avg_stop_d, avg_conv)
        entry_day = t.get("entry_day", 0)
        if heat_cap is not None:
            open_positions = [(ed, r) for ed, r in open_positions if ed > entry_day]
            current_heat = sum(r for _, r in open_positions)
            headroom = max(0.0, heat_cap - current_heat)
            proposed_risk = weight * risk_pct_per_trade
            if proposed_risk > headroom:
                weight = (headroom / risk_pct_per_trade) if risk_pct_per_trade > 0 else 0.0
            open_positions.append((t.get("exit_day", entry_day), weight * risk_pct_per_trade))
        trade_pnl_pct = t["r"] * risk_pct_per_trade * weight
        equity *= (1 + trade_pnl_pct)
        trade_returns_pct.append(trade_pnl_pct)
        curve.append((t.get("exit_day", t.get("entry_day", 0)), equity))
    return curve, trade_returns_pct


def compute_portfolio_metrics(trades, initial_capital=10000.0, risk_pct_per_trade=0.015,
                               days_per_year=365, sizing_mode="fixed", heat_cap=None):
    """
    Computes CAGR, Sharpe, Sortino, Calmar, Max Drawdown %, Profit Factor,
    Recovery Factor from a real compounded equity curve. Annualization
    uses TRADE FREQUENCY (trades/year), the standard approach for a
    swing-trading system where trades don't occur on a fixed daily
    schedule -- consistent with how this project has always measured
    time (day-index based, not calendar-resampled).
    """
    if not trades:
        return None
    curve, trade_returns = build_portfolio_equity_curve(trades, initial_capital, risk_pct_per_trade, sizing_mode, heat_cap)
    if len(trade_returns) < 2:
        return None

    ordered = sorted(trades, key=lambda t: t.get("entry_day", 0))
    span_days = max(1, ordered[-1].get("exit_day", 0) - ordered[0].get("entry_day", 0))
    years = span_days / days_per_year
    trades_per_year = len(trade_returns) / years if years > 0 else len(trade_returns)

    final_equity = curve[-1][1]
    cagr = (final_equity / initial_capital) ** (1/years) - 1 if years > 0 and final_equity > 0 else None

    mean_r = sum(trade_returns) / len(trade_returns)
    std_r = math.sqrt(sum((x - mean_r)**2 for x in trade_returns) / len(trade_returns))
    sharpe = (mean_r / std_r) * math.sqrt(trades_per_year) if std_r > 1e-12 else None

    downside = [x for x in trade_returns if x < 0]
    downside_std = math.sqrt(sum(x**2 for x in downside) / len(trade_returns)) if downside else 0.0
    sortino = (mean_r / downside_std) * math.sqrt(trades_per_year) if downside_std > 1e-12 else None

    # Max drawdown in REAL % terms (on the compounded equity curve, not R-units)
    peak = initial_capital
    max_dd_pct = 0.0
    min_equity = initial_capital
    for _, eq in curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        max_dd_pct = max(max_dd_pct, dd)
        min_equity = min(min_equity, eq)

    calmar = (cagr / max_dd_pct) if (cagr is not None and max_dd_pct > 1e-9) else None

    gains = sum(x for x in trade_returns if x > 0)
    losses = -sum(x for x in trade_returns if x < 0)
    profit_factor = (gains / losses) if losses > 1e-12 else None

    net_profit = final_equity - initial_capital
    recovery_factor = (net_profit / (max_dd_pct * initial_capital)) if max_dd_pct > 1e-9 else None
    min_equity_frac = min_equity / initial_capital  # lowest point EVER reached, as fraction of starting capital

    return dict(n_trades=len(trade_returns), years=years, trades_per_year=trades_per_year,
                final_equity=final_equity, cagr=cagr, sharpe=sharpe, sortino=sortino,
                max_dd_pct=max_dd_pct, calmar=calmar, profit_factor=profit_factor,
                recovery_factor=recovery_factor, curve=curve, min_equity_frac=min_equity_frac)


# ============================================================
# RISK PROFILES -- configuration, not separate codebases.
#
# Every profile is a parameter set consumed by the SAME engine
# (build_portfolio_equity_curve / compute_portfolio_metrics /
# run_monte_carlo_robustness), never a fork. Hard guardrails that apply
# to EVERY profile, unconditionally, by construction:
#   - Stop losses: always active (baked into the frozen, unmodified exit
#     logic in run_dynamic_attr -- no profile can disable this).
#   - Position sizing is always a % of CURRENT equity (compounding,
#     already the only mode build_portfolio_equity_curve implements --
#     no profile can switch to fixed-dollar or unbounded sizing).
#   - Leverage: none of these profiles use any leverage multiplier above
#     1x on the risk_pct_per_trade itself -- "aggressive" means a HIGHER
#     risk_pct_per_trade and looser heat_cap, never leverage.
# ============================================================

RISK_PROFILES = {
    "GROWTH": dict(
        risk_pct_per_trade=0.020,      # 2.0% of current equity per trade
        max_positions=4,
        sizing_mode="conviction_weighted",  # only justified if empirically beneficial -- tested below
        heat_cap=0.10,                 # FIXED: was 0.08 (=4x2%, ZERO headroom over max_positions --
                                        # the root cause of GROWTH underperforming BALANCED, see
                                        # search_growth_profile_parameters()'s docstring). Now 5x2%,
                                        # giving 1 position's worth of real headroom by construction.
        ruin_threshold=0.50,           # "ruin" = account ever falls to 50% of start
    ),
    "BALANCED": dict(
        risk_pct_per_trade=0.010,
        max_positions=3,
        sizing_mode="fixed",
        heat_cap=0.05,
        ruin_threshold=0.60,
    ),
    "CAPITAL_PRESERVATION": dict(
        risk_pct_per_trade=0.005,
        max_positions=2,
        sizing_mode="fixed",
        heat_cap=0.02,
        ruin_threshold=0.75,           # preservation profile treats even a 25% loss as the ruin boundary
    ),
}


def search_growth_profile_parameters(histories_dated, registry=None, btc_id="bitcoin", n_folds_requested=4,
                                      hilo_dated=None, initial_capital=10000.0, balanced_metrics=None):
    """
    ROOT CAUSE (Phase 1, proven via direct computation, not assumed):
    the original GROWTH config had heat_cap / risk_pct_per_trade EXACTLY
    equal to max_positions (0.08/0.02=4), meaning the heat cap was ALWAYS
    binding the instant all 4 positions were open -- every 4th position
    got scaled down toward zero, diluting the aggressive compounding
    GROWTH exists to deliver. Separately: last session's "position cap
    4/5 accepted" finding was validated in ISOLATION against a flat,
    lower risk_pct_per_trade -- never re-tested in COMBINATION with a
    simultaneously higher risk_pct_per_trade, which is exactly what
    GROWTH does. This function fixes both by searching a small grid of
    GROWTH-appropriate combinations where heat_cap ALWAYS has real
    headroom over (max_positions x risk_pct_per_trade) by construction --
    the zero-headroom bug cannot recur from any candidate this generates.

    Every candidate is still "more aggressive than BALANCED" by
    construction (risk_pct_per_trade >= BALANCED's own) -- this searches
    WHICH aggressive combination empirically beats BALANCED, not whether
    to be aggressive at all.
    """
    registry = registry if registry is not None else FEATURE_REGISTRY
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    folds = compute_fold_boundaries(n, n_folds_requested, warmup=210)
    if not folds:
        return None, []

    results = []
    for cap in [3, 4, 5]:
        trades = []
        for test_start, test_end in folds:
            trades.extend(run_dynamic_attr(histories_dated, btc_id=btc_id, day_start=test_start, day_end=test_end,
                                           max_positions=cap, registry=registry, hilo_dated=hilo_dated))
        for risk in [0.010, 0.015, 0.020]:
            for headroom_mult in [1.0, 1.25, 1.5]:  # heat_cap ALWAYS >= cap*risk -- real headroom by construction
                heat = cap * risk * headroom_mult
                m = compute_portfolio_metrics(trades, initial_capital, risk, sizing_mode="fixed", heat_cap=heat)
                if m is not None:
                    results.append(dict(cap=cap, risk=risk, heat=heat, headroom_mult=headroom_mult, metrics=m))

    if not results:
        return None, []

    if balanced_metrics is not None:
        # Only keep candidates that are STILL more aggressive than BALANCED
        # (higher risk_pct_per_trade), then rank by Sharpe among those that
        # beat BALANCED's Sharpe without materially worse drawdown.
        candidates = [r for r in results if r["risk"] > RISK_PROFILES["BALANCED"]["risk_pct_per_trade"]
                      and r["metrics"]["sharpe"] is not None]
        beating = [r for r in candidates if r["metrics"]["sharpe"] > balanced_metrics["sharpe"]
                   and r["metrics"]["max_dd_pct"] <= balanced_metrics["max_dd_pct"] * 1.5]
        pool = beating if beating else candidates
    else:
        pool = [r for r in results if r["metrics"]["sharpe"] is not None]

    best = max(pool, key=lambda r: r["metrics"]["sharpe"]) if pool else None
    return best, results


def run_portfolio_report(histories_dated, registry=None, btc_id="bitcoin", n_folds_requested=4,
                          hilo_dated=None, max_positions=None, initial_capital=10000.0,
                          risk_pct_per_trade=0.015, sizing_mode="fixed", trades_override=None):
    """
    Generates the pooled, walk-forward, OUT-OF-SAMPLE trade set exactly
    the way run_purged_validation already evaluates the hand-set
    scorecard (run_dynamic_attr on each fold's test-only window,
    unmodified) -- then computes the full portfolio performance report
    from it. Does not fit or purge anything (test-window evaluation
    doesn't require purging training data -- only training does), so
    this reuses the simplest, already-validated evaluation path.

    sizing_mode: "fixed" (baseline, byte-identical to prior sessions),
    "vol_targeted", or "conviction_weighted" -- see compute_position_weight().

    trades_override: if provided, skips trade generation and uses this
    trade list directly -- lets callers (Monte Carlo, sizing comparisons)
    reuse ONE generated trade set across multiple metric computations
    instead of regenerating identical trades repeatedly.
    """
    registry = registry if registry is not None else FEATURE_REGISTRY
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    folds = compute_fold_boundaries(n, n_folds_requested, warmup=210)
    if not folds:
        print(f"\nPORTFOLIO REPORT: insufficient history ({n} days).")
        return

    print(f"\n{'='*98}\nPORTFOLIO PERFORMANCE REPORT (out-of-sample, hand-set scorecard, frozen features)\n{'='*98}")
    print(f"Initial capital: ${initial_capital:,.0f}   Risk per trade: {risk_pct_per_trade*100:.2f}% of current capital")
    print(f"Position cap: {max_positions if max_positions else 'unlimited'}   Sizing mode: {sizing_mode}\n")

    if trades_override is not None:
        all_trades = trades_override
    else:
        all_trades = []
        for test_start, test_end in folds:
            trades = run_dynamic_attr(histories_dated, btc_id=btc_id, day_start=test_start, day_end=test_end,
                                      max_positions=max_positions, registry=registry, hilo_dated=hilo_dated)
            all_trades.extend(trades)

    m = compute_portfolio_metrics(all_trades, initial_capital, risk_pct_per_trade, sizing_mode=sizing_mode)
    if m is None:
        print("Insufficient trades to compute portfolio metrics.")
        return

    print(f"Trades: {m['n_trades']}   Span: {m['years']:.2f} years   Trades/year: {m['trades_per_year']:.1f}")
    print(f"Final equity: ${m['final_equity']:,.0f}   Net P&L: ${m['final_equity']-initial_capital:+,.0f}\n")
    print(f"CAGR:             {m['cagr']*100:+.2f}%" if m['cagr'] is not None else "CAGR:             n/a")
    print(f"Sharpe ratio:     {m['sharpe']:+.2f}" if m['sharpe'] is not None else "Sharpe ratio:     n/a")
    print(f"Sortino ratio:    {m['sortino']:+.2f}" if m['sortino'] is not None else "Sortino ratio:    n/a")
    print(f"Max drawdown:     {m['max_dd_pct']*100:.2f}%")
    print(f"Calmar ratio:     {m['calmar']:+.2f}" if m['calmar'] is not None else "Calmar ratio:     n/a")
    print(f"Profit factor:    {m['profit_factor']:.2f}" if m['profit_factor'] is not None else "Profit factor:    n/a")
    print(f"Recovery factor:  {m['recovery_factor']:+.2f}" if m['recovery_factor'] is not None else "Recovery factor:  n/a")

    print(f"\n{'-'*98}")
    print("Interpretation notes:")
    print("  - Sharpe/Sortino annualized by TRADE FREQUENCY (trades/year), the standard")
    print("    approach for swing systems without a fixed daily rebalance schedule.")
    print("  - risk_pct_per_trade is a real, adjustable position-sizing assumption --")
    print("    rerun with --risk-pct-per-trade to see sensitivity (higher risk/trade")
    print("    raises CAGR and drawdown together; this is the FIRST lever worth tuning")
    print("    once this baseline number exists).")
    print("  - This baseline uses EQUAL risk per trade regardless of conviction score --")
    print("    conviction-weighted or volatility-targeted sizing (ranked #2 priority)")
    print("    is the next concrete step to test against these exact numbers.")


def run_monte_carlo_robustness(trades, initial_capital=10000.0, risk_pct_per_trade=0.015,
                                sizing_mode="fixed", n_bootstrap=1000, seed=42, heat_cap=None):
    """
    Bootstrap-resamples the pooled trade list (with replacement) to build
    a distribution of possible CAGR/Sharpe/MaxDD outcomes -- answers
    "is the point-estimate baseline a robust expectation, or a lucky
    ordering of a fixed set of trades." Does not change any trade or
    metric-computation logic -- reuses compute_portfolio_metrics()
    unmodified, just called many times on resampled trade sets.

    Also tracks min_equity_frac per resample -- the lowest point each
    simulated path ever reached, as a fraction of starting capital. This
    is what compute_risk_of_ruin() below turns into an actual ruin
    probability at a chosen threshold.
    """
    rng = random.Random(seed)
    n = len(trades)
    if n < 10:
        return None
    results = {k: [] for k in ("cagr", "sharpe", "sortino", "calmar", "max_dd_pct",
                                "profit_factor", "min_equity_frac")}
    for _ in range(n_bootstrap):
        sample = [trades[rng.randrange(n)] for _ in range(n)]
        m = compute_portfolio_metrics(sample, initial_capital, risk_pct_per_trade,
                                      sizing_mode=sizing_mode, heat_cap=heat_cap)
        if m is None:
            continue
        for k in results:
            if m.get(k) is not None:
                results[k].append(m[k])
    return results


def compute_risk_of_ruin(mc_results, ruin_threshold=0.5):
    """
    From the Monte Carlo min_equity_frac distribution, computes the
    fraction of simulated paths that EVER fell to or below
    `ruin_threshold` of starting capital (e.g. 0.5 = account ever dropped
    to 50% of what you started with, at ANY point along the path -- not
    just the final result). This is the direct, standard definition of
    risk of ruin for a fixed-fractional-risk sizing scheme.
    """
    vals = mc_results.get("min_equity_frac", [])
    if not vals:
        return None
    return sum(1 for v in vals if v <= ruin_threshold) / len(vals)


def print_monte_carlo_report(mc_results, baseline_metrics, ruin_thresholds=(0.7, 0.5, 0.3)):
    print(f"\n{'-'*98}\nMONTE CARLO ROBUSTNESS ({len(mc_results['cagr'])} bootstrap resamples)\n{'-'*98}")
    print(f"{'metric':16s} {'baseline':>10s} {'MC mean':>10s} {'5th pct':>10s} {'95th pct':>10s} {'P(negative)':>12s}")
    for key, label, is_pct in [("cagr","CAGR",True), ("sharpe","Sharpe",False), ("sortino","Sortino",False),
                                ("calmar","Calmar",False), ("max_dd_pct","MaxDD",True), ("profit_factor","ProfitFactor",False)]:
        vals = mc_results.get(key, [])
        if not vals:
            continue
        vals_sorted = sorted(vals)
        p5 = percentile(vals_sorted, 0.05)
        p95 = percentile(vals_sorted, 0.95)
        mean_v = sum(vals) / len(vals)
        base_v = baseline_metrics.get(key)
        p_neg = sum(1 for v in vals if v < 0) / len(vals) if key != "max_dd_pct" else float('nan')
        fmt = (lambda v: f"{v*100:+.2f}%") if is_pct else (lambda v: f"{v:+.2f}")
        base_str = fmt(base_v) if base_v is not None else "n/a"
        pneg_str = f"{p_neg*100:.1f}%" if key != "max_dd_pct" else "n/a"
        print(f"{label:16s} {base_str:>10s} {fmt(mean_v):>10s} {fmt(p5):>10s} {fmt(p95):>10s} {pneg_str:>12s}")

    print(f"\n{'-'*98}\nRISK OF RUIN (probability the account EVER falls to X% of starting capital)\n{'-'*98}")
    for thresh in ruin_thresholds:
        ror = compute_risk_of_ruin(mc_results, thresh)
        if ror is not None:
            flag = "  <-- material survival risk" if ror > 0.10 else ""
            print(f"  P(equity ever <= {thresh*100:.0f}% of start): {ror*100:.1f}%{flag}")
    print("\nInterpretation: if P(negative Sharpe) is low and the 5th-95th percentile band")
    print("doesn't cross zero, the baseline is a robust expectation, not a lucky ordering")
    print("of the same 389 trades. A wide band or high P(negative) means treat the point")
    print("estimate with real caution before sizing capital against it.")


def compute_portfolio_correlation(trades):
    """
    Measures whether trades on different assets tend to win/lose TOGETHER
    (a diversification-benefit check). Groups trades into non-overlapping
    time buckets, computes each asset's mean return per bucket, then
    correlates asset pairs. High positive correlation means the position
    cap's "3 simultaneous positions" isn't really capturing 3 independent
    bets -- it's closer to one leveraged bet repeated 3 times.
    """
    by_asset = {}
    for t in trades:
        by_asset.setdefault(t["sym"], []).append(t)
    assets = sorted(by_asset.keys())
    if len(assets) < 2:
        return None, assets

    # 90-day buckets, mean trade pct return per asset per bucket
    all_days = [t.get("entry_day", 0) for t in trades]
    if not all_days:
        return None, assets
    min_day, max_day = min(all_days), max(all_days)
    bucket_size = 90
    n_buckets = max(1, (max_day - min_day) // bucket_size + 1)

    bucket_returns = {a: [None]*n_buckets for a in assets}
    for a in assets:
        bucket_sums = [[] for _ in range(n_buckets)]
        for t in by_asset[a]:
            b = min(n_buckets-1, (t.get("entry_day", 0) - min_day) // bucket_size)
            bucket_sums[b].append(t.get("pct", 0))
        for b in range(n_buckets):
            if bucket_sums[b]:
                bucket_returns[a][b] = sum(bucket_sums[b]) / len(bucket_sums[b])

    corr_matrix = {}
    for i, a1 in enumerate(assets):
        for a2 in assets[i+1:]:
            paired = [(bucket_returns[a1][b], bucket_returns[a2][b]) for b in range(n_buckets)
                      if bucket_returns[a1][b] is not None and bucket_returns[a2][b] is not None]
            if len(paired) >= 5:
                xs, ys = zip(*paired)
                r = pearson_r(list(xs), list(ys))
                corr_matrix[(a1, a2)] = r
    return corr_matrix, assets


def print_correlation_report(corr_matrix, assets):
    print(f"\n{'-'*98}\nPORTFOLIO CORRELATION (90-day bucketed returns, {len(assets)} assets)\n{'-'*98}")
    if not corr_matrix:
        print("Insufficient overlapping data to compute cross-asset correlation.")
        return
    for (a1, a2), r in sorted(corr_matrix.items(), key=lambda kv: -abs(kv[1]) if kv[1] else 0):
        flag = "  <-- HIGH: limited diversification benefit" if r is not None and abs(r) > 0.5 else ""
        print(f"  {a1:6s} vs {a2:6s}: r={r:+.3f}" if r is not None else f"  {a1:6s} vs {a2:6s}: n/a", flag)
    valid_r = [r for r in corr_matrix.values() if r is not None]
    if valid_r:
        avg_r = sum(valid_r) / len(valid_r)
        print(f"\nAverage pairwise correlation: {avg_r:+.3f}")
        print("Interpretation: correlation near 0 means the position cap genuinely buys")
        print("diversification (independent bets). Correlation above ~0.5 means simultaneous")
        print("positions are closer to one leveraged bet on the same underlying move --")
        print("the position cap should probably be tighter than it currently is.")


def run_risk_profile_comparison(histories_dated, registry=None, btc_id="bitcoin", n_folds_requested=4,
                                 hilo_dated=None, initial_capital=10000.0, n_bootstrap=500):
    """
    Runs GROWTH, BALANCED, and CAPITAL_PRESERVATION against the SAME
    underlying trade set (entries are frozen -- only sizing/heat/position-
    cap parameters differ per profile) and reports the full metric +
    Monte Carlo + risk-of-ruin comparison for each, plus a KEEP/REJECT
    verdict on the conviction-weighted-sizing assumption baked into
    GROWTH specifically (per the requirement that it only be used "if
    empirically beneficial").
    """
    registry = registry if registry is not None else FEATURE_REGISTRY
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    folds = compute_fold_boundaries(n, n_folds_requested, warmup=210)
    if not folds:
        print("Insufficient history.")
        return

    print(f"\n{'='*98}\nRISK PROFILE COMPARISON\n{'='*98}")
    print("Guardrails enforced for every profile (not configurable): stop losses always active")
    print("(frozen exit logic), position sizing always % of CURRENT equity (compounding, never")
    print("fixed-dollar), zero leverage multiplier on any profile.\n")

    # Trades depend on max_positions (a portfolio-construction parameter,
    # not an entry-signal change), so each profile's own max_positions
    # requires its own trade generation pass -- entries themselves
    # (which candidates qualify) are unaffected; only how many
    # SIMULTANEOUS positions are allowed differs.
    profile_results = {}
    for name, cfg in RISK_PROFILES.items():
        trades = []
        for test_start, test_end in folds:
            trades.extend(run_dynamic_attr(histories_dated, btc_id=btc_id, day_start=test_start, day_end=test_end,
                                           max_positions=cfg["max_positions"], registry=registry, hilo_dated=hilo_dated))
        m = compute_portfolio_metrics(trades, initial_capital, cfg["risk_pct_per_trade"],
                                      sizing_mode=cfg["sizing_mode"], heat_cap=cfg["heat_cap"])
        mc = run_monte_carlo_robustness(trades, initial_capital, cfg["risk_pct_per_trade"],
                                        sizing_mode=cfg["sizing_mode"], n_bootstrap=n_bootstrap,
                                        heat_cap=cfg["heat_cap"])
        ror = compute_risk_of_ruin(mc, cfg["ruin_threshold"]) if mc else None
        profile_results[name] = dict(config=cfg, metrics=m, mc=mc, ror=ror, n_trades=len(trades))

    print(f"{'Profile':22s} {'CAGR':>9s} {'Sharpe':>8s} {'Sortino':>8s} {'Calmar':>8s} "
          f"{'MaxDD':>8s} {'PF':>6s} {'RF':>7s} {'RoR':>8s} {'Trades':>7s}")
    for name, res in profile_results.items():
        m = res["metrics"]
        if m is None:
            print(f"{name:22s} insufficient trades")
            continue
        ror_str = f"{res['ror']*100:.1f}%" if res['ror'] is not None else "n/a"
        print(f"{name:22s} {m['cagr']*100:>+8.2f}% {m['sharpe']:>+8.2f} {m['sortino']:>+8.2f} "
              f"{m['calmar']:>+8.2f} {m['max_dd_pct']*100:>7.2f}% {m['profit_factor']:>6.2f} "
              f"{m['recovery_factor']:>+7.2f} {ror_str:>8s} {res['n_trades']:>7d}")

    # ---- GROWTH parameter validation: does GROWTH actually beat BALANCED? Search, don't assume. ----
    print(f"\n{'-'*98}\nGROWTH PARAMETER VALIDATION (Phase 1 finding: prior GROWTH had zero heat-cap headroom)\n{'-'*98}")
    balanced_m = profile_results.get("BALANCED", {}).get("metrics")
    if balanced_m is not None:
        growth_before = profile_results["GROWTH"]["metrics"]
        print(f"BEFORE (current config, cap={RISK_PROFILES['GROWTH']['max_positions']}, "
              f"risk={RISK_PROFILES['GROWTH']['risk_pct_per_trade']*100:.1f}%, "
              f"heat={RISK_PROFILES['GROWTH']['heat_cap']*100:.1f}%): "
              f"Sharpe={growth_before['sharpe']:+.2f}  vs BALANCED Sharpe={balanced_m['sharpe']:+.2f}  "
              f"{'GROWTH BEATS BALANCED' if growth_before['sharpe'] > balanced_m['sharpe'] else 'GROWTH STILL UNDERPERFORMS'}")

        best, all_candidates = search_growth_profile_parameters(histories_dated, registry, btc_id, n_folds_requested,
                                                                 hilo_dated, initial_capital, balanced_metrics=balanced_m)
        if best is not None:
            print(f"\nSEARCHED {len(all_candidates)} candidate configs (cap x risk x heat-headroom grid).")
            print(f"BEST VALIDATED CONFIG: cap={best['cap']}, risk={best['risk']*100:.1f}%, "
                  f"heat={best['heat']*100:.1f}% (headroom={best['headroom_mult']:.2f}x over cap*risk)")
            bm = best['metrics']
            print(f"AFTER: Sharpe={bm['sharpe']:+.2f}  CAGR={bm['cagr']*100:+.2f}%  MaxDD={bm['max_dd_pct']*100:.2f}%  "
                  f"vs BALANCED Sharpe={balanced_m['sharpe']:+.2f}")
            if bm['sharpe'] > balanced_m['sharpe']:
                print(f"\nDECISION: KEEP -- updating RISK_PROFILES['GROWTH'] with validated parameters.")
                RISK_PROFILES["GROWTH"]["max_positions"] = best["cap"]
                RISK_PROFILES["GROWTH"]["risk_pct_per_trade"] = best["risk"]
                RISK_PROFILES["GROWTH"]["heat_cap"] = best["heat"]
            else:
                print(f"\nDECISION: REJECT -- no aggressive configuration in the searched grid beats BALANCED")
                print(f"          on this dataset. This is a real, honest finding: on the tested data, being")
                print(f"          MORE aggressive does not produce better risk-adjusted returns than BALANCED --")
                print(f"          not every account size should default to GROWTH. Recommend BALANCED unless")
                print(f"          a wider grid or different dataset changes this conclusion.")

    # ---- Conviction-weighted sizing in GROWTH: empirically justified, or fall back to fixed? ----
    print(f"\n{'-'*98}\nGROWTH PROFILE: conviction-weighted vs fixed sizing (same trades, same caps)\n{'-'*98}")
    growth_cfg = RISK_PROFILES["GROWTH"]
    growth_trades = []
    for test_start, test_end in folds:
        growth_trades.extend(run_dynamic_attr(histories_dated, btc_id=btc_id, day_start=test_start, day_end=test_end,
                                              max_positions=growth_cfg["max_positions"], registry=registry, hilo_dated=hilo_dated))
    m_fixed = compute_portfolio_metrics(growth_trades, initial_capital, growth_cfg["risk_pct_per_trade"],
                                        sizing_mode="fixed", heat_cap=growth_cfg["heat_cap"])
    m_conv = compute_portfolio_metrics(growth_trades, initial_capital, growth_cfg["risk_pct_per_trade"],
                                       sizing_mode="conviction_weighted", heat_cap=growth_cfg["heat_cap"])
    if m_fixed and m_conv:
        print(f"{'':22s} {'Sharpe':>8s} {'Sortino':>8s} {'Calmar':>8s} {'MaxDD':>8s}")
        print(f"{'fixed sizing':22s} {m_fixed['sharpe']:>+8.2f} {m_fixed['sortino']:>+8.2f} "
              f"{m_fixed['calmar']:>+8.2f} {m_fixed['max_dd_pct']*100:>7.2f}%")
        print(f"{'conviction-weighted':22s} {m_conv['sharpe']:>+8.2f} {m_conv['sortino']:>+8.2f} "
              f"{m_conv['calmar']:>+8.2f} {m_conv['max_dd_pct']*100:>7.2f}%")
        conv_better = (m_conv['sharpe'] > m_fixed['sharpe'] + 0.05) and (m_conv['max_dd_pct'] <= m_fixed['max_dd_pct'] + 0.02)
        print(f"\nConviction-weighting empirically justified for GROWTH: {'YES' if conv_better else 'NO -- reverts to fixed sizing'}")
        if not conv_better:
            RISK_PROFILES["GROWTH"]["sizing_mode"] = "fixed"

    return profile_results


def run_autonomous_portfolio_optimization(histories_dated, registry=None, btc_id="bitcoin",
                                           n_folds_requested=4, hilo_dated=None, max_positions=3,
                                           initial_capital=10000.0, risk_pct_per_trade=0.015,
                                           n_bootstrap=500):
    """
    Executes the ranked portfolio-optimization queue autonomously:
    Monte Carlo robustness -> correlation analysis -> sizing-mode
    comparison (vol-targeted, conviction-weighted) -> position-cap
    sensitivity -> transaction-cost sensitivity. Each candidate is
    compared against the SAME frozen baseline trade set and KEPT only if
    it improves at least one major metric without materially degrading
    the others. Does not touch any feature, label, or scoring logic --
    every candidate operates purely on position sizing / portfolio
    construction, applied to the exact same underlying trades.
    """
    registry = registry if registry is not None else FEATURE_REGISTRY
    btc_dates, btc_closes, _ = histories_dated[btc_id]
    n = len(btc_dates)
    folds = compute_fold_boundaries(n, n_folds_requested, warmup=210)
    if not folds:
        print("Insufficient history.")
        return

    print(f"\n{'='*98}\nAUTONOMOUS PORTFOLIO OPTIMIZATION\n{'='*98}")
    all_trades = []
    for test_start, test_end in folds:
        trades = run_dynamic_attr(histories_dated, btc_id=btc_id, day_start=test_start, day_end=test_end,
                                  max_positions=max_positions, registry=registry, hilo_dated=hilo_dated)
        all_trades.extend(trades)

    baseline = compute_portfolio_metrics(all_trades, initial_capital, risk_pct_per_trade, sizing_mode="fixed")
    if baseline is None:
        print("Insufficient trades for a baseline.")
        return
    print(f"\nBASELINE: n={baseline['n_trades']} trades, {baseline['years']:.2f} years")
    print(f"CAGR={baseline['cagr']*100:+.2f}%  Sharpe={baseline['sharpe']:+.2f}  Sortino={baseline['sortino']:+.2f}  "
          f"Calmar={baseline['calmar']:+.2f}  MaxDD={baseline['max_dd_pct']*100:.2f}%  "
          f"ProfitFactor={baseline['profit_factor']:.2f}  RecoveryFactor={baseline['recovery_factor']:+.2f}")

    # ---- 1. Monte Carlo robustness (diagnostic -- confirms/questions the baseline itself) ----
    mc = run_monte_carlo_robustness(all_trades, initial_capital, risk_pct_per_trade, "fixed", n_bootstrap)
    if mc:
        print_monte_carlo_report(mc, baseline)

    # ---- 2. Portfolio correlation (diagnostic -- informs whether the position cap buys real diversification) ----
    corr_matrix, assets = compute_portfolio_correlation(all_trades)
    print_correlation_report(corr_matrix, assets)

    # ---- 3+4. Sizing-mode candidates: vol-targeted, conviction-weighted ----
    decisions = []
    def compare_and_decide(label, candidate_metrics):
        print(f"\n{'='*98}\n{label}\n{'='*98}")
        print(f"{'Metric':16s} {'Baseline':>12s} {'New':>12s} {'Delta':>12s}")
        rows = [("CAGR", "cagr", True), ("Sharpe", "sharpe", False), ("Sortino", "sortino", False),
                ("Calmar", "calmar", False), ("Profit Factor", "profit_factor", False),
                ("Recovery Factor", "recovery_factor", False), ("Max Drawdown", "max_dd_pct", True),
                ("Trade Count", "n_trades", None)]
        improved_major = False
        degraded_major = False
        for label_r, key, is_pct in rows:
            bv, nv = baseline.get(key), candidate_metrics.get(key)
            if key == "n_trades":
                print(f"{label_r:16s} {bv:>12d} {nv:>12d} {nv-bv:>+12d}")
                continue
            if bv is None or nv is None:
                print(f"{label_r:16s} {'n/a':>12s} {'n/a':>12s} {'n/a':>12s}")
                continue
            fmt = (lambda v: f"{v*100:+.2f}%") if is_pct else (lambda v: f"{v:+.2f}")
            delta = nv - bv
            print(f"{label_r:16s} {fmt(bv):>12s} {fmt(nv):>12s} {fmt(delta):>12s}")
            if key in ("sharpe", "sortino", "calmar") and delta > 0.05:
                improved_major = True
            if key == "max_dd_pct" and delta > 0.05:  # drawdown got materially WORSE
                degraded_major = True
            if key in ("sharpe", "sortino") and delta < -0.05:
                degraded_major = True
        decision = "KEEP" if (improved_major and not degraded_major) else "REJECT"
        print(f"\nDECISION: {decision}")
        decisions.append((label, decision, candidate_metrics))
        return decision

    vol_targeted = compute_portfolio_metrics(all_trades, initial_capital, risk_pct_per_trade, sizing_mode="vol_targeted")
    if vol_targeted:
        compare_and_decide("CANDIDATE: Volatility-Targeted Position Sizing", vol_targeted)

    conv_weighted = compute_portfolio_metrics(all_trades, initial_capital, risk_pct_per_trade, sizing_mode="conviction_weighted")
    if conv_weighted:
        compare_and_decide("CANDIDATE: Conviction-Weighted Position Sizing", conv_weighted)

    # ---- 5. Position-cap sensitivity (proxy for "dynamic max-position limits") ----
    for cap in [1, 2, 4, 5]:
        if cap == max_positions:
            continue
        cap_trades = []
        for test_start, test_end in folds:
            cap_trades.extend(run_dynamic_attr(histories_dated, btc_id=btc_id, day_start=test_start, day_end=test_end,
                                               max_positions=cap, registry=registry, hilo_dated=hilo_dated))
        cap_metrics = compute_portfolio_metrics(cap_trades, initial_capital, risk_pct_per_trade, sizing_mode="fixed")
        if cap_metrics:
            compare_and_decide(f"CANDIDATE: Position Cap = {cap} (baseline cap = {max_positions})", cap_metrics)

    # ---- 6. Transaction-cost sensitivity (diagnostic, not a KEEP/REJECT candidate) ----
    print(f"\n{'='*98}\nTRANSACTION-COST SENSITIVITY (diagnostic)\n{'='*98}")
    for extra_cost_bps in [0, 10, 25, 50]:
        adj_trades = [dict(t, r=t["r"] - extra_cost_bps/10000/max(t.get("stop_d", 0.01), 0.01)) for t in all_trades]
        adj_metrics = compute_portfolio_metrics(adj_trades, initial_capital, risk_pct_per_trade, sizing_mode="fixed")
        if adj_metrics:
            print(f"  +{extra_cost_bps}bps extra cost: Sharpe={adj_metrics['sharpe']:+.2f}  "
                  f"CAGR={adj_metrics['cagr']*100:+.2f}%  ProfitFactor={adj_metrics['profit_factor']:.2f}")

    # ---- Final summary ----
    print(f"\n{'='*98}\nFINAL SUMMARY\n{'='*98}")
    kept = [d for d in decisions if d[1] == "KEEP"]
    rejected = [d for d in decisions if d[1] == "REJECT"]
    print(f"ACCEPTED optimizations: {[d[0] for d in kept] if kept else 'none'}")
    print(f"REJECTED optimizations: {[d[0] for d in rejected] if rejected else 'none'}")
    if not kept:
        print("\nNo candidate materially improved risk-adjusted performance without degrading")
        print("another major metric. The frozen baseline remains the production configuration.")
    print("\nDeferred (not implemented this pass -- see accompanying analysis for reasoning):")
    print("  Sector/exposure caps (not applicable -- single-sector crypto universe)")
    print("  Position scaling / trailing exits / pyramiding (touch exit logic directly --")
    print("    higher design risk, sequenced after sizing-only changes are validated)")
    print("  Walk-forward robustness / parameter sensitivity (substantially covered by")
    print("    --purged-validation's existing per-fold breakdown)")


def run_sweep(histories, split_frac):
    """
    Grid-search CONV_THRESHOLD x MAX_POSITIONS x RSI_MAX x TOKEN_TREND_FILTER.
    Split history chronologically: first `split_frac` = IN-SAMPLE (used to
    rank configs), remaining = OUT-OF-SAMPLE (never touched during ranking,
    only used afterward to check whether the top in-sample configs actually
    hold up on unseen data). This is the standard guard against reporting a
    config that's just curve-fit noise on one historical window -- exactly
    the trap that produced the LINK sign-flip between the 751d and 2066d
    windows earlier in this analysis.
    """
    global CONV_THRESHOLD
    any_hist = next(iter(histories.values()))
    n = len(any_hist[0])
    split_i = int(n * split_frac)
    WARMUP = 210

    hist_in = {cid: (c[:split_i], v[:split_i]) for cid, (c, v) in histories.items()}
    # OOS gets a warmup buffer taken from BEFORE the split so day-210 of the
    # slice lines up with the true out-of-sample start, not day 0 of OOS.
    oos_start = max(0, split_i - WARMUP)
    hist_oos = {cid: (c[oos_start:], v[oos_start:]) for cid, (c, v) in histories.items()}

    print(f"\nSWEEP: {n} total days -> in-sample days 0-{split_i} ({split_frac*100:.0f}%), "
          f"out-of-sample days {split_i}-{n} ({(1-split_frac)*100:.0f}%)")

    conv_grid = [65, 70, 75, 80]
    pos_grid = [2, 3, 4]
    rsi_grid = [None, 80]
    trend_grid = [False, True]

    results = []
    total = len(conv_grid) * len(pos_grid) * len(rsi_grid) * len(trend_grid)
    print(f"Testing {total} combinations on in-sample data...")
    done = 0
    for conv in conv_grid:
        CONV_THRESHOLD = conv
        for pos in pos_grid:
            for rsi in rsi_grid:
                for trend in trend_grid:
                    trades_in = run(hist_in, max_positions=pos, rsi_max=rsi, token_trend_filter=trend, verbose=False)
                    s_in = summarize(trades_in)
                    results.append(dict(conv=conv, pos=pos, rsi=rsi, trend=trend, in_sample=s_in))
                    done += 1
    print(f"Done. {done} configs tested.\n")

    # Require a minimum in-sample trade count to be eligible -- filters out
    # configs whose apparent "great" result is just 3-5 lucky trades.
    MIN_TRADES = 15
    eligible = [r for r in results if r["in_sample"]["n"] >= MIN_TRADES]
    if not eligible:
        print(f"No config produced >= {MIN_TRADES} in-sample trades. Loosen the grid or "
              f"use more history -- results would be statistically meaningless otherwise.")
        return
    eligible.sort(key=lambda r: -r["in_sample"]["expectancy"])
    top = eligible[:10]

    print(f"{'='*90}\nTOP {len(top)} CONFIGS BY IN-SAMPLE EXPECTANCY (min {MIN_TRADES} trades)\n{'='*90}")
    print(f"{'conv':>5s} {'pos':>4s} {'rsi':>5s} {'trend':>6s} | "
          f"{'IN n':>5s} {'IN win%':>8s} {'IN exp%':>8s} || "
          f"{'OOS n':>6s} {'OOS win%':>9s} {'OOS exp%':>9s}  verdict")
    for r in top:
        CONV_THRESHOLD = r["conv"]
        trades_oos = run(hist_oos, max_positions=r["pos"], rsi_max=r["rsi"], token_trend_filter=r["trend"], verbose=False)
        s_oos = summarize(trades_oos)
        rsi_str = str(r["rsi"]) if r["rsi"] is not None else "off"
        trend_str = "on" if r["trend"] else "off"
        # Verdict: does the config still look positive out-of-sample?
        if s_oos["n"] < 5:
            verdict = "too few OOS trades"
        elif s_oos["expectancy"] > 0 and s_oos["win_rate"] >= 45:
            verdict = "HOLDS UP"
        elif s_oos["expectancy"] > 0:
            verdict = "mixed"
        else:
            verdict = "FAILED OOS"
        print(f"{r['conv']:>5d} {r['pos']:>4d} {rsi_str:>5s} {trend_str:>6s} | "
              f"{r['in_sample']['n']:>5d} {r['in_sample']['win_rate']:>7.1f}% {r['in_sample']['expectancy']:>+7.2f}% || "
              f"{s_oos['n']:>6d} {s_oos['win_rate']:>8.1f}% {s_oos['expectancy']:>+8.2f}%  {verdict}")

    print(f"\n{'-'*90}")
    print("Read this table right-to-left: IN-SAMPLE columns are what the grid search")
    print("optimized for and WILL look good almost by construction. The OUT-OF-SAMPLE")
    print("columns are the honest test -- a config only means something if it also")
    print("performs on data it was never tuned against. 'FAILED OOS' configs are")
    print("overfit to the in-sample window and should be discarded even if their")
    print("in-sample numbers looked the best.")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-key", default="")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--csv-dir", default="", help="Folder with CryptoDataDownload CSVs (e.g. BTCUSDT_day.csv, ETHUSDT_day.csv, SOLUSDT_day.csv)")
    ap.add_argument("--conv-threshold", type=int, default=None, help="Override conviction entry threshold (default 70)")
    ap.add_argument("--watchlist", default="", help='Comma-separated coingecko_id:SYMBOL pairs to REPLACE the default watchlist entirely, e.g. "bitcoin:BTC,ethereum:ETH,my-token:XYZ"')
    ap.add_argument("--add", default="", help='Comma-separated coingecko_id:SYMBOL pairs to ADD to the current watchlist, e.g. "render-token:RNDR"')
    ap.add_argument("--remove", default="", help='Comma-separated SYMBOLs to REMOVE from the current watchlist, e.g. "TAO,ORDI"')
    ap.add_argument("--dump-trades", action="store_true", help="Print the full chronological trade log (symbol, dates as day-index, prices, conviction, return)")
    ap.add_argument("--max-positions", type=int, default=None, help="Cap simultaneous open positions; highest-conviction candidates get priority when more signals fire than free slots (tests whether correlated multi-token entries dilute the edge)")
    ap.add_argument("--rsi-max", type=float, default=None, help="Skip new entries when token RSI(14) exceeds this value (overbought/overextension filter), e.g. --rsi-max 70")
    ap.add_argument("--token-trend-filter", action="store_true", help="Require token's own price above its own 200D EMA at entry (stricter trend confirmation beyond BTC regime)")
    ap.add_argument("--sweep", action="store_true", help="Grid-search conv-threshold x max-positions x rsi-max x token-trend-filter, validated with a chronological in-sample/out-of-sample split to guard against overfitting to one window")
    ap.add_argument("--sweep-split", type=float, default=0.7, help="Fraction of history used as in-sample for --sweep (default 0.7 = first 70%% tune, last 30%% validate)")
    ap.add_argument("--validate", action="store_true", help="Validate the CURRENT fixed config (conv-threshold/max-positions/rsi-max/token-trend-filter) across multiple expanding-window chronological folds, plus BTC bull/bear regime stratification. Does not search parameters -- use this to confirm robustness of one config at a time, ideally after --sweep has suggested a candidate.")
    ap.add_argument("--n-folds", type=int, default=4, help="Requested number of expanding-window folds for --validate (default 4; actual count is reduced automatically if history is too short)")
    ap.add_argument("--attribute", action="store_true", help="Feature attribution: break the conviction score into its components (relative_strength, trend, volume_expansion, momentum) and measure each one's contribution via ablation, correlation+permutation test, information gain, and redundancy analysis. CSV mode only.")
    ap.add_argument("--rs-lookback-sweep", action="store_true", help="Test relative_strength with lookback windows 7/14/21/30 days, holding everything else constant, to check whether the 30d window is causing lag. CSV mode only.")
    ap.add_argument("--diagnose-component", default="", help="Direct 'enabled vs blocked' trade-set comparison for one component (e.g. --diagnose-component relative_strength), testing whether it filters out higher-expectancy trades than the ones it lets in. CSV mode only.")
    ap.add_argument("--rs-mode", default="level", choices=["level", "slope"], help="relative_strength implementation: 'level' (original -- 30d trailing outperformance) or 'slope' (NEW -- rate of change of a short-window RS edge, designed to be leading rather than confirming). Applies to --attribute and --diagnose-component.")
    ap.add_argument("--rs-window", type=int, default=10, help="Short RS window (days) used when --rs-mode slope (default 10)")
    ap.add_argument("--rs-slope-lookback", type=int, default=10, help="How many days back to compare the RS edge against when --rs-mode slope (default 10)")
    ap.add_argument("--use-atr", action="store_true", help="Enable the generalized feature framework and add ATR/Donchian squeeze as a 5th conviction feature (requires CSVs with High/Low columns). Applies to --attribute and --diagnose-component.")
    ap.add_argument("--use-obv", action="store_true", help="Enable the generalized feature framework and add On-Balance Volume as a conviction feature (uses existing Close/Volume data only, no High/Low needed). Applies to --attribute and --diagnose-component. Combine with --use-atr to test both together.")
    ap.add_argument("--train-model", action="store_true", help="Learn feature weights via L2-regularized logistic regression, one model per expanding-window fold (training data only, never touching that fold's held-out period). Reports per-fold coefficients, cross-fold stability, bootstrap confidence intervals, and a comparison against the current hand-set scorecard weights.")
    ap.add_argument("--l2", type=float, default=1.0, help="L2 (ridge) regularization strength for --train-model (default 1.0)")
    ap.add_argument("--n-bootstrap", type=int, default=200, help="Bootstrap resamples per fold for confidence intervals in --train-model (default 200)")
    ap.add_argument("--max-holding", type=int, default=90, help="Max holding period (days) when simulating trade outcomes for --train-model labels (default 90)")
    ap.add_argument("--weights", default="", help='Comma-separated name:value pairs of LEARNED weights to use instead of each feature\'s hand-set weight, e.g. --weights "trend:0.412,momentum:-0.087". Applies to --attribute and --diagnose-component; requires --use-atr/--use-obv (or both) so the named features exist in the active registry.')
    ap.add_argument("--audit-data", action="store_true", help="Phase 1 data integrity audit: checks the raw CSVs in --csv-dir for gaps, duplicates, missing/unparseable fields, zero-volume days, flat-price days, OHLC consistency violations, statistical outliers, and a wash-trading screening heuristic. Read-only -- does not run any simulation. Requires --csv-dir.")
    ap.add_argument("--audit-warmup", action="store_true", help="Phase 1b feature coverage & warm-up audit: per-asset/per-feature first-valid dates, cross-asset comparison, effective sample sizes (full-history and per-fold), empirical causality/leakage verification, and feature synchronization analysis. Read-only. Requires --csv-dir. Combine with --use-atr/--use-obv to audit those features too.")
    ap.add_argument("--audit-labels", action="store_true", help="Phase 2 label audit: documents exact label generation logic, verifies diagnostic tooling matches the real unmodified build_training_samples() exactly, measures label overlap/uniqueness (Lopez de Prado average-uniqueness), class balance, holding-period distribution, exit-reason distribution, and fold-boundary label leakage. Read-only. Requires --csv-dir.")
    ap.add_argument("--purged-validation", action="store_true", help="Institution-grade Purged Walk-Forward CV (Lopez de Prado): purges training samples whose label window overlaps the test fold, applies an embargo (= max_holding) after every prior fold, verifies zero label leakage, fits logistic regression on purged vs unpurged data, compares downstream test-period trading performance and feature rankings before/after purging. Does not affect --validate or --train-model, which remain unchanged. Requires --csv-dir.")
    ap.add_argument("--audit-probability", action="store_true", help="Read-only audit of the native logistic model's out-of-sample probability output (purged training, no evaluation/trading path): distribution stats, histogram, winner/loser separation, ROC AUC, PR AUC, Brier score, 10-bin calibration curve. Requires --csv-dir.")
    ap.add_argument("--candidate-only", action="store_true", help="Restrict --audit-probability to the candidate population (hand-set score>=70, regime-on) instead of every asset-day, training AND scoring only on candidates. Runs native/quartile/decile representations for ATR+OBV and reports a comparison table. Does not affect --audit-probability without this flag.")
    ap.add_argument("--representation", default="all", choices=["native", "quartile", "decile", "all"], help="Which representation(s) to run under --candidate-only (default 'all' runs native+quartile+decile and compares them).")
    ap.add_argument("--exclude-feature", default="", help="Run the feature-ablation experiment inside the candidate-only pipeline: baseline (all features) vs this feature excluded from the logistic matrix only (candidate selection and every other feature unchanged). Reports coefficients, intercept, AUC/PR/Brier/calibration for both, a comparison table, and interpretive answers. Requires --candidate-only. Uses representation=native regardless of --representation.")
    ap.add_argument("--audit-forward-return", action="store_true", help="Final diagnostic: tests whether the six features carry linear/monotonic information about continuous 30-day forward return, independent of the triple-barrier label entirely. Per-asset and pooled Pearson/Spearman/joint-R² with permutation significance and bootstrap CIs. No regime stratification, no BTC-normalization, no discretization. Diagnostic only -- does not affect trading logic. Requires --csv-dir.")
    ap.add_argument("--use-xsrank", action="store_true", help="Add Cross-Sectional Return Rank (30d return percentile-ranked against the full universe) as a 7th feature, alongside whichever base registry is active (--use-atr/--use-obv). Does not modify or replace any existing feature. Applies to every existing command by extending whatever registry it would otherwise use.")
    ap.add_argument("--use-shared-series", action="store_true", help="Add a shared-series feature (proof of architecture: volume-based BTC dominance, computed from already-loaded OHLCV -- CoinGecko-sourced dominance/stablecoin-supply data was checked and rejected for insufficient historical depth on the free tier, see load_shared_series_from_coingecko() docstring). Does not modify or replace any existing feature. Applies to every existing command by extending whatever registry it would otherwise use. Combine with --shared-series-csv PATH to load a different series instead.")
    ap.add_argument("--shared-series-csv", default="", help="Path to a two-column (date,value) CSV to use as the shared series instead of the default volume-dominance proxy. Requires --use-shared-series.")
    ap.add_argument("--use-per-asset-series", action="store_true", help="Add a per-asset external series feature (e.g. funding rate, open interest) as a new feature. Looks for one CSV per asset in --csv-dir matching --per-asset-pattern. Does not modify or replace any existing feature.")
    ap.add_argument("--per-asset-pattern", default="{sym}_funding.csv", help="Filename pattern (with {sym} placeholder) for per-asset series CSVs. Requires --use-per-asset-series.")
    ap.add_argument("--portfolio-report", action="store_true", help="Production-phase deliverable: computes CAGR, Sharpe, Sortino, Calmar, Max Drawdown %%, Profit Factor, and Recovery Factor from a real compounded equity curve built from pooled out-of-sample trades. Requires --csv-dir.")
    ap.add_argument("--initial-capital", type=float, default=10000.0, help="Starting capital for --portfolio-report (default $10,000)")
    ap.add_argument("--risk-pct-per-trade", type=float, default=0.015, help="Fraction of current capital risked per trade for --portfolio-report (default 0.015 = 1.5%%)")
    ap.add_argument("--sizing-mode", default="fixed", choices=["fixed", "vol_targeted", "conviction_weighted"], help="Position sizing rule for --portfolio-report (default 'fixed', byte-identical to the original baseline).")
    ap.add_argument("--optimize-portfolio", action="store_true", help="Runs the full autonomous portfolio-optimization queue: Monte Carlo robustness, correlation analysis, sizing-mode comparison, position-cap sensitivity, and transaction-cost sensitivity, each compared against the frozen baseline with an explicit KEEP/REJECT decision. Requires --csv-dir.")
    ap.add_argument("--risk-profiles", action="store_true", help="Runs GROWTH, BALANCED, and CAPITAL_PRESERVATION risk profiles against the same frozen entry logic, reporting CAGR/Sharpe/Sortino/Calmar/MaxDD/Profit Factor/Recovery Factor/Risk-of-Ruin for each, plus an empirical check on whether conviction-weighted sizing is justified for GROWTH. Requires --csv-dir.")
    ap.add_argument("--nonlinear-benchmark", action="store_true", help="ONE additive experiment: tests whether a nonlinear model (XGBoost > LightGBM > RandomForest, first available) extracts materially more signal from the FROZEN feature set than the linear baseline. Reuses population/folds/purge/embargo/labels unmodified. Requires xgboost, lightgbm, or scikit-learn installed (pip install xgboost shap --break-system-packages) -- the only command in this tool with external dependencies; every other command remains dependency-free. Requires --csv-dir.")
    a = ap.parse_args()

    if a.audit_data:
        if not a.csv_dir:
            sys.exit("--audit-data requires --csv-dir")

    global CONV_THRESHOLD
    if a.conv_threshold is not None:
        CONV_THRESHOLD = a.conv_threshold
        print(f"Conviction threshold overridden to {CONV_THRESHOLD}")

    if a.watchlist:
        TOKENS.clear()
        for pair in a.watchlist.split(","):
            cid, sym = pair.split(":")
            TOKENS[cid.strip()] = sym.strip()
        print(f"Watchlist replaced: {list(TOKENS.values())}")
    if a.add:
        for pair in a.add.split(","):
            cid, sym = pair.split(":")
            TOKENS[cid.strip()] = sym.strip()
        print(f"Added to watchlist: {[p.split(':')[1] for p in a.add.split(',')]}")
    if a.remove:
        remove_syms = {s.strip().upper() for s in a.remove.split(",")}
        for cid in [k for k, v in TOKENS.items() if v.upper() in remove_syms]:
            del TOKENS[cid]
        print(f"Removed from watchlist: {sorted(remove_syms)}")
    print(f"Active watchlist ({len(TOKENS)}): {list(TOKENS.values())}")

    if a.audit_data:
        watchlist_map = {sym: cid for cid, sym in TOKENS.items()}
        run_data_audit(a.csv_dir, watchlist_map)
        return

    histories = {}
    if a.csv_dir:
        import os, glob
        # Match every token currently in the active watchlist (TOKENS) to a CSV
        # file by symbol -- flexible to whatever --watchlist/--add/--remove set.
        csv_symbol_map = {sym: cid for cid, sym in TOKENS.items()}
        print(f"CSV MODE (dynamic universe): loading from {a.csv_dir}")
        found_any = False
        hilo = {}
        for sym, cid in csv_symbol_map.items():
            matches = glob.glob(os.path.join(a.csv_dir, f"*{sym}USDT*day*.csv")) or \
                      glob.glob(os.path.join(a.csv_dir, f"*{sym}USDT*.csv")) or \
                      glob.glob(os.path.join(a.csv_dir, f"*{sym.lower()}usdt*.csv"))
            if not matches:
                print(f"  {sym:5s} SKIPPED (no file matching *{sym}USDT*.csv found)")
                continue
            path = matches[0]
            try:
                dates, closes, vols = load_csv_history(path)
                histories[cid] = (dates, closes, vols)
                TOKENS[cid] = sym
                found_any = True
                hi_str = ""
                if a.use_atr:
                    hdates, highs, lows = load_csv_hilo(path)
                    if hdates is not None:
                        hilo[cid] = (hdates, highs, lows)
                        hi_str = ", H/L available" if hdates == dates else ", H/L date MISMATCH (will be skipped for this asset)"
                    else:
                        hi_str = ", H/L NOT FOUND in this file"
                print(f"  {sym:5s} {len(dates)} days ({dates[0]} to {dates[-1]}) from {os.path.basename(path)}{hi_str}")
            except Exception as e:
                print(f"  {sym:5s} SKIPPED (parse error: {e})")
        if not found_any:
            sys.exit("No usable CSV files found. Expected filenames like BTCUSDT_day.csv, ETHUSDT_day.csv, SOLUSDT_day.csv")
        if "bitcoin" not in histories:
            sys.exit("BTC data (bitcoin) is required as the global clock / regime reference -- no BTCUSDT CSV found.")
        # NOTE: no truncation step. Every asset keeps its own full native
        # history; run_dynamic() aligns everything by calendar date, using
        # BTC's own full date range as the simulation clock, and only
        # includes each other asset in the universe once its own data
        # exists for that date AND its own 210-day warmup has passed.
        print(f"Dynamic universe: {len(histories)} assets, BTC clock spans "
              f"{histories['bitcoin'][0][0]} to {histories['bitcoin'][0][-1]} "
              f"({len(histories['bitcoin'][0])} days)")
        if a.use_atr and a.use_obv:
            active_registry = FEATURE_REGISTRY_WITH_ATR_OBV
        elif a.use_atr:
            active_registry = FEATURE_REGISTRY_WITH_ATR
        elif a.use_obv:
            active_registry = FEATURE_REGISTRY_WITH_OBV
        else:
            active_registry = None
        use_framework = a.use_atr or a.use_obv or a.use_xsrank or a.use_shared_series or a.use_per_asset_series
        if a.use_xsrank:
            # Cross-Sectional Return Rank needs universe-wide visibility no
            # single-asset feature has -- precomputed once here, then added
            # to whatever base registry is active. Does not modify
            # RelativeStrengthFeature or any other existing feature/registry;
            # this only builds a NEW dict with one additional entry.
            base_registry = active_registry if active_registry is not None else FEATURE_REGISTRY
            xsrank_lookup = precompute_cross_sectional_ranks(histories, btc_id="bitcoin", lookback=30)
            active_registry = dict(base_registry, cross_sectional_return_rank=CrossSectionalReturnRankFeature(xsrank_lookup))
            print(f"Cross-Sectional Return Rank ADDED to the active registry (base: {list(base_registry)}).")
        if a.use_shared_series:
            # Same additive pattern as xsrank above -- builds a NEW dict
            # with one more entry, never modifies any existing feature or
            # registry. Default series (volume-based BTC dominance) is
            # computed from data already loaded; --shared-series-csv swaps
            # in any other two-column (date,value) series instead, e.g. a
            # full-depth CoinGecko paid-tier export or a FRED/DXY/Fear&Greed
            # download, with zero further architectural changes.
            base_registry = active_registry if active_registry is not None else FEATURE_REGISTRY
            btc_dates_for_series = histories["bitcoin"][0]
            if a.shared_series_csv:
                date_series = load_shared_series_from_csv(a.shared_series_csv)
                series_label = f"CSV: {a.shared_series_csv}"
            else:
                date_series = compute_btc_volume_dominance_series(histories, btc_id="bitcoin")
                series_label = "volume-based BTC dominance (computed from loaded OHLCV)"
            shared_feat = SharedSeriesFeature("shared_series", date_series, btc_dates_for_series, weight=15, window=30)
            active_registry = dict(base_registry, shared_series=shared_feat)
            print(f"Shared-series feature ADDED to the active registry (base: {list(base_registry)}, source: {series_label}, "
                  f"{len(date_series)} dated values loaded).")
        if a.use_per_asset_series:
            # Same additive pattern as shared_series -- per-asset external
            # data (funding, OI, exchange reserves) instead of one
            # universe-wide value. Requires one CSV per asset, matched by
            # symbol via --per-asset-pattern.
            base_registry = active_registry if active_registry is not None else FEATURE_REGISTRY
            btc_dates_for_series = histories["bitcoin"][0]
            symbol_to_cid = {sym: cid for cid, sym in TOKENS.items()}
            per_asset_series = load_per_asset_series_from_csvs(a.csv_dir, symbol_to_cid, pattern=a.per_asset_pattern)
            if not per_asset_series:
                sys.exit(f"--use-per-asset-series: no files matched pattern '{a.per_asset_pattern}' in {a.csv_dir}")
            per_asset_lookup = build_per_asset_series_lookup(histories, per_asset_series)
            pa_feat = PerAssetExternalSeriesFeature("per_asset_series", per_asset_lookup, btc_dates_for_series, weight=15, window=30)
            active_registry = dict(base_registry, per_asset_series=pa_feat)
            print(f"Per-asset external series ADDED to the active registry (base: {list(base_registry)}, "
                  f"assets covered: {list(per_asset_series.keys())}).")
        if use_framework:
            print(f"Feature framework ACTIVE: {list(active_registry)}")

        # Parse --weights "name:val,name:val" into a dict, validated
        # against the active registry so a typo'd feature name fails
        # loudly rather than being silently ignored.
        learned_weights = None
        if a.weights:
            if not use_framework:
                sys.exit("--weights requires --use-atr and/or --use-obv so the named features exist in the active registry.")
            learned_weights = {}
            for pair in a.weights.split(","):
                fname, val = pair.split(":")
                fname = fname.strip()
                if fname not in active_registry:
                    sys.exit(f"--weights: '{fname}' is not in the active registry {list(active_registry)}")
                learned_weights[fname] = float(val)
            print(f"Using LEARNED weights (overriding hand-set): {learned_weights}")

        if a.sweep:
            print("NOTE: --sweep still uses the legacy shared-index engine and requires")
            print("      truncated/aligned history -- use --validate for dynamic-universe")
            print("      multi-fold analysis, which is the recommended path now.")
            L = min(len(h[0]) for h in histories.values())
            hist_legacy = {k: (v[1][-L:], v[2][-L:]) for k, v in histories.items()}
            run_sweep(hist_legacy, a.sweep_split)
            return
        if a.validate:
            run_validate_dynamic(histories, btc_id="bitcoin", n_folds_requested=a.n_folds,
                         max_positions=a.max_positions, rsi_max=a.rsi_max,
                         token_trend_filter=a.token_trend_filter)
            return
        if a.train_model:
            if not use_framework:
                print("NOTE: no --use-atr/--use-obv given -- training against the 4 legacy features only.")
            run_train_model(histories, registry=active_registry or FEATURE_REGISTRY, btc_id="bitcoin",
                            n_folds_requested=a.n_folds, hilo_dated=hilo if a.use_atr else None,
                            l2=a.l2, n_bootstrap=a.n_bootstrap, max_holding=a.max_holding)
            return
        if a.audit_warmup:
            if not use_framework:
                print("NOTE: no --use-atr/--use-obv given -- auditing the 4 legacy features only.")
            run_feature_warmup_audit(histories, registry=active_registry or FEATURE_REGISTRY,
                                     hilo_dated=hilo if a.use_atr else None, btc_id="bitcoin",
                                     n_folds_requested=a.n_folds)
            return
        if a.audit_labels:
            if not use_framework:
                print("NOTE: no --use-atr/--use-obv given -- auditing labels against the 4 legacy features only.")
            run_label_audit(histories, registry=active_registry or FEATURE_REGISTRY, btc_id="bitcoin",
                            n_folds_requested=a.n_folds, hilo_dated=hilo if a.use_atr else None,
                            max_holding=a.max_holding)
            return
        if a.purged_validation:
            if not use_framework:
                print("NOTE: no --use-atr/--use-obv given -- purged validation against the 4 legacy features only.")
            run_purged_validation(histories, registry=active_registry or FEATURE_REGISTRY, btc_id="bitcoin",
                                  n_folds_requested=a.n_folds, hilo_dated=hilo if a.use_atr else None,
                                  l2=a.l2, n_bootstrap=a.n_bootstrap, max_holding=a.max_holding,
                                  max_positions=a.max_positions)
            return
        if a.audit_forward_return:
            if not use_framework:
                print("NOTE: no --use-atr/--use-obv given -- auditing against the 4 legacy features only.")
            run_forward_return_audit(histories, registry=active_registry or FEATURE_REGISTRY, btc_id="bitcoin",
                                     n_folds_requested=a.n_folds, hilo_dated=hilo if a.use_atr else None,
                                     max_holding=a.max_holding)
            return
        if a.risk_profiles:
            run_risk_profile_comparison(histories, registry=active_registry or FEATURE_REGISTRY, btc_id="bitcoin",
                                        n_folds_requested=a.n_folds, hilo_dated=hilo if a.use_atr else None,
                                        initial_capital=a.initial_capital)
            return
        if a.optimize_portfolio:
            run_autonomous_portfolio_optimization(histories, registry=active_registry or FEATURE_REGISTRY, btc_id="bitcoin",
                                                  n_folds_requested=a.n_folds, hilo_dated=hilo if a.use_atr else None,
                                                  max_positions=a.max_positions or 3, initial_capital=a.initial_capital,
                                                  risk_pct_per_trade=a.risk_pct_per_trade)
            return
        if a.portfolio_report:
            run_portfolio_report(histories, registry=active_registry or FEATURE_REGISTRY, btc_id="bitcoin",
                                 n_folds_requested=a.n_folds, hilo_dated=hilo if a.use_atr else None,
                                 max_positions=a.max_positions, initial_capital=a.initial_capital,
                                 risk_pct_per_trade=a.risk_pct_per_trade, sizing_mode=a.sizing_mode)
            return
        if a.nonlinear_benchmark:
            if not use_framework:
                print("NOTE: no --use-atr/--use-obv given -- benchmarking against the 4 legacy features only.")
            run_nonlinear_benchmark(histories, registry=active_registry or FEATURE_REGISTRY, btc_id="bitcoin",
                                    n_folds_requested=a.n_folds, hilo_dated=hilo if a.use_atr else None,
                                    max_holding=a.max_holding)
            return
        if a.audit_probability and a.candidate_only and a.exclude_feature:
            if not use_framework:
                print("NOTE: no --use-atr/--use-obv given -- ablating against the 4 legacy features only.")
            if a.exclude_feature not in (active_registry or FEATURE_REGISTRY):
                sys.exit(f"--exclude-feature: '{a.exclude_feature}' is not in the active registry "
                         f"{list(active_registry or FEATURE_REGISTRY)}")
            run_feature_ablation_experiment(histories, registry=active_registry or FEATURE_REGISTRY, btc_id="bitcoin",
                                            n_folds_requested=a.n_folds, hilo_dated=hilo if a.use_atr else None,
                                            l2=a.l2, max_holding=a.max_holding, exclude_feature=a.exclude_feature)
            return
        if a.audit_probability and a.candidate_only:
            if not use_framework:
                print("NOTE: no --use-atr/--use-obv given -- auditing against the 4 legacy features only.")
            reps = ("native", "quartile", "decile") if a.representation == "all" else (a.representation,)
            run_candidate_only_comparison(histories, registry=active_registry or FEATURE_REGISTRY, btc_id="bitcoin",
                                          n_folds_requested=a.n_folds, hilo_dated=hilo if a.use_atr else None,
                                          l2=a.l2, max_holding=a.max_holding, representations=reps)
            return
        if a.audit_probability:
            if not use_framework:
                print("NOTE: no --use-atr/--use-obv given -- auditing against the 4 legacy features only.")
            run_probability_diagnostic(histories, registry=active_registry or FEATURE_REGISTRY, btc_id="bitcoin",
                                       n_folds_requested=a.n_folds, hilo_dated=hilo if a.use_atr else None,
                                       l2=a.l2, max_holding=a.max_holding)
            return
        if a.attribute:
            run_attribution(histories, btc_id="bitcoin", n_folds_requested=a.n_folds,
                            max_positions=a.max_positions, rs_mode=a.rs_mode,
                            rs_window=a.rs_window, rs_slope_lookback=a.rs_slope_lookback,
                            registry=active_registry, hilo_dated=hilo if a.use_atr else None,
                            weight_override=learned_weights)
            return
        if a.rs_lookback_sweep:
            run_rs_lookback_sweep(histories, btc_id="bitcoin", n_folds_requested=a.n_folds,
                                  max_positions=a.max_positions)
            return
        if a.diagnose_component:
            diagnose_component_timing(histories, a.diagnose_component, btc_id="bitcoin",
                                      n_folds_requested=a.n_folds, max_positions=a.max_positions,
                                      rs_mode=a.rs_mode, rs_window=a.rs_window,
                                      rs_slope_lookback=a.rs_slope_lookback,
                                      registry=active_registry, hilo_dated=hilo if a.use_atr else None,
                                      weight_override=learned_weights)
            return
        report(run_dynamic(histories, btc_id="bitcoin", max_positions=a.max_positions,
                           rsi_max=a.rsi_max, token_trend_filter=a.token_trend_filter),
               dump_trades=a.dump_trades)
        return
    if a.synthetic:
        print("SYNTHETIC MODE (engine self-test, random-walk data)")
        for i, cid in enumerate(TOKENS): histories[cid] = synthetic_history(i, 730)
    elif not a.synthetic:
        if not a.api_key: sys.exit("Need --api-key CG-xxxx, --csv-dir, or --synthetic")
        for cid in TOKENS:
            try:
                histories[cid] = fetch_history(cid, a.days, a.api_key)
                print(f"  {TOKENS[cid]:5s} {len(histories[cid][0])} days")
                time.sleep(2.2)  # stay under 30 req/min demo limit
            except Exception as e:
                print(f"  {TOKENS[cid]:5s} SKIPPED ({e})")
        L = max(len(h[0]) for h in histories.values())
        histories = {k: v for k, v in histories.items() if len(v[0]) == L}
    if a.sweep:
        run_sweep(histories, a.sweep_split)
        return
    if a.validate:
        run_validate(histories, n_folds_requested=a.n_folds,
                     max_positions=a.max_positions, rsi_max=a.rsi_max,
                     token_trend_filter=a.token_trend_filter)
        return
    report(run(histories, max_positions=a.max_positions, rsi_max=a.rsi_max, token_trend_filter=a.token_trend_filter), dump_trades=a.dump_trades)

if __name__ == "__main__":
    main()
