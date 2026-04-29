# Contributing

Quick-start for working on Hermes Kite locally.

## Setup

```bash
git clone https://github.com/hash02/hermes-kite.git
cd hermes-kite
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install                      # ruff + mypy + json-check on every commit
```

Python 3.11+ required. The project is a regular installable package — every
worker and engine module imports via `from engine.policy import …` or
`from funds.aave_usdc_worker import …`. No `sys.path` hacks (CI enforces).

## Daily commands

```bash
# Tests (stdlib only, no pytest needed)
python -m unittest discover tests

# Coverage (pyproject configures sources + branch mode)
coverage run -m unittest discover tests
coverage report

# Lint
ruff check                              # 0 errors before push
ruff check --fix                        # auto-fix what's safe

# Format
ruff format .                           # apply
ruff format --check .                   # verify (CI runs this)

# Types — currently checks engine/ only; workers are gradually typed
mypy engine

# Reconcile (book-only when not on Kite RPC)
python scripts/reconcile.py --skip-onchain
```

## Project layout

```
config/        policy.json — fund allocations + worker knobs
data/          committed snapshots: portfolio_summary, kite_settled, nav_ledger, agent_registry
docs/          QUICKSTART, runbooks/, adr/
engine/        framework — policy, risk_engine, nav_accounting, backtest, fund_router, grid_base, yield_base
funds/         strategy workers (one *_worker.py per scanner)
onchain/       Kite testnet executor + agent passport registration
scripts/       operational CLIs (reconcile, watchdog, export_csv)
tests/         unit tests (unittest, stdlib)
```

## Editing policy.json

`config/policy.json` is the single source of truth for fund allocations,
sleeve sizing, and per-worker thresholds. Edit it, re-run any worker — the
new values take effect next cycle. CI validates the JSON on every PR.

When `risk.engine_enabled` is `true`, `engine/risk_engine.py` overrides the
static `principal_usd` values with vol-targeted / Kelly-scaled / drawdown-halted
sizes. See `engine/risk_engine.py --show --enable-preview` for what the
sizes would become.

## Adding a new worker

1. Drop `funds/your_worker.py` modeled on an existing one (pick the closest
   strategy category — yield, grid, momentum, etc.).
2. Add the worker entry under the relevant fund + sleeve in
   `config/policy.json`.
3. Add an entry in `engine/risk_engine._WORKER_META` (category +
   counterparty) — the risk engine and backtest both depend on it.
4. Tag your positions with `worker`, `fund`, and `sleeve` keys so the
   fund_router can attribute them.
5. Write a unit test in `tests/test_<your_worker>.py`.
6. `ruff format .` + `mypy engine` (won't touch your worker yet) +
   `python -m unittest discover tests` before opening a PR.

## Commit messages

Conventional-ish prefixes — `feat(area):`, `fix(area):`, `refactor(area):`,
`docs:`, `chore:`, `test:`. Body explains *why*. CI auto-verifies tests +
lint + format + types on every push.

## Pull requests

- Open as draft until CI is green.
- Each PR should be one logical change (the engineering-practices sweep was
  split into 5 phase-PRs for this reason).
- Reference the runbook(s) you updated, if any.
- For changes touching `data/*.json`, run `python scripts/reconcile.py
  --skip-onchain` locally and paste the output in the description.
