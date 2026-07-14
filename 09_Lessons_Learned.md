# 09. Lessons Learned

This document summarizes engineering and research lessons drawn from the
development, correction, and freeze process of the Turtle QTS Research
Engine.

## Research Before Optimization

The project's own research process (culminating in the OHLCV feature
family being permanently retired, see `05_Research_Findings.md`) ran to
completion, including a nonlinear-model benchmark against the same frozen
feature set, before any portfolio-level optimization or parameter tuning
was pursued. Testing whether an edge exists at all, before optimizing
around an assumed edge, avoided over-engineering a system with no
demonstrated statistical basis.

## Estimate-Correcting Work Is High Priority Despite Adding No Edge

Corrections C1 through C6 do not raise expected returns; several (C1,
C3's reporting fix, C4, the accepted portion of C5) mechanically lower
the measured edge by removing an optimistic bias. This work was treated
as high priority regardless, because deploying capital against a
backtest known to be biased upward is a distinct and serious risk from
simply having no edge. Correcting the estimate, even when the correction
is unflattering, was treated as more valuable than pursuing new edge on
top of an unverified baseline.

## Separate Research From Execution

The engine intentionally contains no live execution, order routing, or
broker integration. Every correction to trade accounting, cost modeling,
and risk sizing was made and verified entirely within the research/
backtesting boundary. The C6 final verification made explicit that a
control-flow decision that is correct for a closed-loop backtest
("preserve position state during a data gap") is not automatically
correct for a live system with an external, independently-changing
broker state. Keeping research and execution as separate concerns made
that mismatch identifiable rather than silently inherited by a future
live system.

## Avoid Feature Creep

The C5 correction process is a direct example: an engine-level daily
liquidity filter was implemented, audited, and found to be an
undisclosed strategy redesign rather than a bias correction, because it
changed which trades occurred rather than correcting how already-decided
trades were measured. It was reverted in favor of a read-only diagnostic
that reports the same information without altering trading behavior. The
lesson generalizes: a fix scoped to "correct a defect" must be verified
to leave trade selection unchanged, or it has silently become a strategy
change.

## Architecture Freeze

Freezing the architecture before starting defect remediation (rather than
after) constrained every correction to be implemented inside the existing
structure -- as an additional gate, an additional aggregation step, or a
corrected formula -- rather than as a restructuring. This made each
correction's blast radius auditable: for every C1-C6 item, it was
possible to demonstrate exactly which lines changed and to run a
before/after comparison holding everything else constant.

## Validation-First Development

Every correction in this project followed the same sequence: implement,
run the regression suite, produce a before/after comparison, and only
then move to the next item -- and for C3, C4, and C5, a dedicated
mathematical or scope audit step was inserted before declaring the
correction frozen. Two of those audits (C3, C4) found genuine defects in
the first implementation that a before/after metrics table alone did not
surface: C3's initial fix collapsed cashflow timing in a way that looked
like a legitimate result (lower Sharpe) until directly decomposed; C4's
initial fix had a look-ahead bias that did not show up in any trade-count
or win-rate check, only in a direct inspection of what data was available
at each decision point. Metrics comparisons alone were not sufficient;
targeted mathematical verification caught what aggregate statistics did
not.
