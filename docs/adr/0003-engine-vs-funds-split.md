# ADR 0003 — `engine/` vs `funds/` split

* **Status**: Accepted
* **Date**: 2026-04-29
* **Context**: Before this split, `funds/` held both the strategy workers (`*_worker.py`) AND the framework engines (`policy.py`, `risk_engine.py`, `nav_accounting.py`, `backtest.py`, `grid_base.py`, `yield_base.py`, `fund_router.py`). Tests and scripts imported via `sys.path.insert(0, "funds/")` because `funds/` wasn't a real package.

## Decision

Split into two packages:

* **`engine/`** — framework. Anything a worker *calls* lives here. Modules: `policy`, `risk_engine`, `nav_accounting`, `backtest`, `fund_router`, `grid_base`, `yield_base`, `logging_setup`.
* **`funds/`** — strategy workers, one per scanner. Each worker is thin: declare `WORKER_NAME`, `SLEEVE_TARGETS = sleeve_targets_for(WORKER_NAME) or _FALLBACK_TARGETS`, fetch loop, decide, upsert position.

Both directories have `__init__.py` — they are real Python packages, importable via absolute imports. The `sys.path.insert(funds/)` pattern is gone.

## Considered alternatives

1. **Keep `funds/` flat.** Simplest. Path hacks remain. Mental model "strategy vs framework" stays muddy. Adding a worker means scanning the same dir for examples mixed with framework modules.
2. **Move framework into a `core/` directory.** Same outcome with a different name. `engine/` reads more naturally given the system has a "risk engine" + "backtest engine" + "NAV engine".
3. **Single `hermes_kite/` package** with `engine/` and `funds/` as subpackages. Tighter — every import becomes `from hermes_kite.engine.policy import …`. More verbose; gains nothing for a single-deployment project.

## Why `engine/` + `funds/`

* **Mental clarity.** "Where does the framework end and the strategy begin?" becomes a directory boundary.
* **Worker thinness enforced by example.** Every file in `funds/` is a config + a fetch loop + decision logic. Anything else is an `engine/` module — workers don't write to `data/`, don't talk to RPC, don't compute NAV.
* **Test surface clarification.** `tests/test_<name>.py` patterns make it obvious what's being tested: framework or strategy.
* **mypy can be selective.** `mypy engine` runs in CI today; workers stay relaxed because each one is mostly I/O glue. Splitting the dirs makes the policy easy to express.

## What stayed in funds/

`fund_router.py` was a borderline call — it's wiring (reads positions, attributes to sleeves) more than strategy. Decision: moved to `engine/fund_router.py` because it imports `engine.policy.fund_router_config()` and is consumed by ops scripts, not by other workers. Workers in `funds/` should not need to know the router exists.

## Consequences

* **Every worker's import block changed.** One-time cost, paid in PR #1 of the engineering sweep.
* **Tests updated** to use absolute imports `from engine import …` and `from scripts import reconcile`. No more `sys.path.insert`.
* **Scripts self-bootstrap.** `scripts/export_csv.py` and `scripts/watchdog.py` add `sys.path.insert(0, REPO_ROOT)` so they work without `pip install -e .`. CI installs anyway; the bootstrap is just for direct-from-clone ergonomics.
* **`pyproject.toml`'s `setuptools.packages.find`** lists `engine*, funds*` as installable, excludes `tests*, scripts*, onchain*`. Only the framework + strategy code becomes import targets when someone does `pip install hermes-kite`; ops scripts and tests are not part of the package surface.

## Out of scope

* Splitting `engine/` further into sub-packages (`engine.risk/`, `engine.fund/`, etc.). Considered, rejected — module count is small enough that flat `engine/*` is searchable.
* Vendoring or packaging worker dependencies. Workers depend on `requests`, `urllib`, and json — all stdlib or single deps. No need for a vendor dir.
