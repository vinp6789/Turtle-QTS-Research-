# 08. Known Limitations

This document records unavoidable limitations of the frozen Turtle QTS
Research Engine. Per project decision, no solutions or mitigations are
proposed here; limitations are documented as-is.

## Survivorship Bias

The token universe supplied to the engine (via `DEFAULT_TOKENS`, CLI
watchlist flags, or the set of CSV files a user provides) is chosen with
present-day knowledge of which tokens still exist and are considered
relevant. C5's accepted correction (`run_survivorship_dependence_report`)
quantifies dependence on within-universe liquidity tiers but cannot
correct for tokens that existed and failed before ever being considered
for inclusion in the universe. This requires a true point-in-time
listing/delisting database, which is explicitly outside this engine's
scope.

## Exchange Outages

The engine has no connection to any live exchange, broker, or venue. Its
C6 data-integrity gate detects malformed, gapped, or stale data within
the backtester and prevents new trading decisions on it, but it cannot
detect, respond to, or recover from an actual exchange or venue outage,
since no such live connection exists in this codebase.

## Custody Risk

The engine has no concept of custody, wallet balances, exchange account
balances, or asset control. Loss of funds through exchange insolvency,
key compromise, or any other custody failure is entirely outside what
this engine can model, detect, or protect against.

## Live Execution Not Included

This is a backtesting and research engine only. It contains no order
placement, no order management, no broker/exchange API integration
beyond read-only historical/market-data fetching (CoinGecko), and no
mechanism for reconciling its internal position state against a live
venue's actual position state. The C6 final verification found that the
"skip evaluation, preserve position" behavior is correct for a closed-loop
backtest but would create a state-mismatch risk if the same code path
were used as a live control loop.

## Capacity Assumptions

The engine's cost model (transaction fee and slippage, corrected per C1)
uses fixed percentage assumptions independent of order size, order book
depth, or market impact. No capacity, liquidity-consumption, or
market-impact modeling exists in the source.

## Data Dependency

All results depend entirely on the historical data supplied, whether via
CoinGecko API fetch, CSV files, or the synthetic self-test generator. The
engine performs no independent verification that supplied data is
complete, accurate, or representative beyond the C6 data-integrity checks
(malformed values and calendar gaps within a single asset's own series).
Cross-source data quality, exchange-reported volume accuracy, and
historical data revisions are outside this engine's ability to detect.
