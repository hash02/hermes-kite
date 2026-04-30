# Changelog

All notable changes to Hermes Kite. Format loosely follows [Keep a Changelog]; project versioning is calendar-style for now (0.x while pre-stable).

## [Unreleased]

### Added — Engineering-practices sweep
- `pyproject.toml` replaces `requirements.txt`. Project metadata, prod + dev deps, ruff/mypy/coverage tool configs.
- `engine/` package created — framework modules (policy, risk_engine, nav_accounting, backtest, fund_router, grid_base, yield_base, logging_setup) moved out of `funds/`.
- `funds/`, `tests/`, `scripts/` are now real Python packages (have `__init__.py`).
- All `sys.path.insert(funds/)` hacks removed; tests + scripts use absolute `from engine.* import …` (`from scripts.reconcile import …`).
- `.github/workflows/ci.yml` — runs ruff check + ruff format check + mypy on engine/ + coverage run on every push/PR. Validates every committed JSON.
- `.pre-commit-config.yaml` — ruff (check + format), JSON/YAML/TOML validators, end-of-file fixer, mypy on engine/.
- `CONTRIBUTING.md` — daily commands, project layout, how to add a new worker.
- `engine/logging_setup.py` — shared structured-JSON logger with `HERMES_RUN_ID` correlation. Every worker uses `setup_logger(WORKER_NAME)`. Replaces 9 separate `logging.basicConfig` calls.
- `scripts/watchdog.py` — stale/missing/malformed worker status detection. Mirrors `scripts/reconcile.py`'s Report/Finding shape. Exit 0/1 for cron.
- `tests/test_watchdog.py` — 8 tests covering fresh, stale, missing, malformed, naive datetime, missing dir, error-count properties.
- `ARCHITECTURE.md` — high-level design + repo layout + worker contract + why-this-shape.
- `CHANGELOG.md`, `SECURITY.md`.
- `docs/runbooks/`: reconcile-failure, worker-stale, settlement-nonce-mismatch.
- `docs/adr/`: 0001-cron-no-daemons, 0002-policy-driven-config, 0003-engine-vs-funds-split.

### Changed
- Tightened error handling: `except Exception` narrowed to `(OSError, json.JSONDecodeError)` on every JSON load path in engine/ + workers. Cycle-boundary catches against unknown upstream-API error shapes intentionally stay broad and log via `as e:`.
- Lazy import in `engine.policy` → `except ImportError` (was `Exception`).

## 0.2.0 — 2026-04-23

Hackathon submission surface (PR #1, merged to main as `d617c1e`).

### Added — fund operations
- `engine/nav_accounting.py` — proper fund accounting. Unit pricing, mgmt fee + hurdle-lifted perf fee, monthly/quarterly/annual statements, crystallization. Persists to `data/nav_ledger.json`.
- `scripts/reconcile.py` — book-vs-chain drift detector. Book integrity + passport hash + on-chain nonce alignment + per-tx existence. Found a real bug while testing: passport hash recomputation needed compact JSON separators to match `register_agent.py`.
- 38 unit tests for nav_accounting + reconcile.

### Added — risk + backtest
- `engine/risk_engine.py` — vol-targeted + Kelly-scaled + drawdown-halted + counterparty-capped sizing. Off by default (`risk.engine_enabled=false`); `engine.policy.sleeve_targets_for` routes through it when on.
- `engine/backtest.py` — synthetic Monte Carlo over the current policy. Per-fund Sharpe / Sortino / MaxDD / CAGR distributions. `--compare`, `--kelly-sweep`, `--fund` flags.
- 31 unit tests for risk_engine + backtest.

### Added — config + tooling
- `config/policy.json` — single source of truth for fund allocations, sleeve sizing, per-worker thresholds. Workers read via `engine.policy.sleeve_targets_for(name)` and `worker_cfg(name)`.
- `scripts/export_csv.py` — on-demand CSV reporting (funds, sleeves, positions, trades, settlements + manifest). Filters: `--fund`, `--since`.

### Added — workers
- 17/17 configured workers ship: aave_usdc, morpho_usdc, euler_pyusd, sgho, superstate_uscc, delta_neutral_funding, polymarket_btc_updown, pyth_momentum, grid_eth_usdc, grid_btc_usdc, grid_sol, grid_stables, tv_momentum, xstocks_grid, xstocks_directional, crypto_memecoins, wow_sniper_base.
- Per-sleeve sizing across all shipping workers; `fund` + `sleeve` tags on positions.
- Shared `engine.grid_base.GridConfig` engine for the four grid workers.

### Fixed
- 90/10 stablecoin_floor drift collapsed from +302% to +0.6% via per-sleeve sizing refactor.
- 75/25 stablecoin_yield from +60.9% to +0.6%.

## 0.1.0 — 2026-04-23 (initial)

- Repo scaffold + README + LICENSE.
- First 8 settlement marker txs live on Kite testnet.
- Agent passport DID self-attestation tx (`kite-passport:{agent_id}:{payload_hash}`).
- 6 initial workers (aave_usdc, delta_neutral_funding, polymarket_btc_updown, xstocks_grid, xstocks_directional, tv_momentum).

[Keep a Changelog]: https://keepachangelog.com/en/1.1.0/
