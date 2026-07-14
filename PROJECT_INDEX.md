# Project Index

Turtle QTS Research Engine -- Documentation Set (Frozen v1.0)

| Document | Contents |
|---|---|
| [README.md](README.md) | Project overview, objectives, key capabilities, folder structure, quick start, main CLI commands, high-level architecture diagram, current project status. |
| [01_Architecture.md](01_Architecture.md) | Overall architecture, engine flow, dynamic universe, feature framework, validation framework, portfolio engine, risk engine, data flow, module relationships, design decisions, what is intentionally not included. |
| [02_Strategy.md](02_Strategy.md) | Trading philosophy, entry logic, exit logic, position sizing, conviction scoring, feature descriptions, regime filters, assumptions, limitations. |
| [03_Validation.md](03_Validation.md) | Walk-forward validation, purged validation, embargo, bootstrap, Monte Carlo, permutation testing, feature attribution, regime testing, portfolio testing, and why each method exists. |
| [04_Portfolio_Engine.md](04_Portfolio_Engine.md) | Portfolio construction, position sizing, risk profiles, correlation adjustment, heat cap, portfolio metrics, Monte Carlo, risk of ruin, performance reporting. |
| [05_Research_Findings.md](05_Research_Findings.md) | Final OHLCV, indicator, feature-importance, and nonlinear-model conclusions; orthogonal data research status; what was permanently rejected; what remains future work. |
| [06_C1_C6_Corrections.md](06_C1_C6_Corrections.md) | Problem, root cause, implementation, validation, and result for each of the six approved critical corrections. |
| [07_Architecture_Freeze.md](07_Architecture_Freeze.md) | Freeze declaration, frozen components, allowed future changes, explicit non-modification statement. |
| [08_Known_Limitations.md](08_Known_Limitations.md) | Survivorship bias, exchange outages, custody risk, live execution exclusion, capacity assumptions, data dependency. |
| [09_Lessons_Learned.md](09_Lessons_Learned.md) | Engineering and research lessons from the development and correction process. |
| [10_CHANGELOG.md](10_CHANGELOG.md) | Chronological record of the gate review and the C1-C6 correction process. |

## Reading Order

For a first-time reader: `README.md`, `01_Architecture.md`,
`02_Strategy.md`, `05_Research_Findings.md`, `07_Architecture_Freeze.md`.

For an auditor verifying the correction process: `06_C1_C6_Corrections.md`
and `10_CHANGELOG.md`, cross-referenced against `01_Architecture.md` for
the components each correction touched.

For a risk/portfolio reviewer: `04_Portfolio_Engine.md` and
`08_Known_Limitations.md`.

## Status

Frozen v1.0. See `07_Architecture_Freeze.md` for the complete freeze
declaration and the narrow set of changes still permitted.
