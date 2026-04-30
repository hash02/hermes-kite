# Architecture

## One-line summary

Cron-driven scanner workers + shared state file + framework engines + on-chain settlement markers. No daemons, no shared mutable state across hosts, every cycle is idempotent and restart-safe.

## Big picture

```
                            policy.json
                          (single source of truth
                           — fund + sleeve + worker
                           knobs)
                                 │
                                 ▼
                       engine.policy.sleeve_targets_for(name)
                                 │
                       (optionally routed through
                        engine.risk_engine.apply_engine
                        when risk.engine_enabled=true)
                                 │
                                 ▼
   ┌──────────────┐   per-cycle   ┌──────────────────────────┐
   │   cron tick  │──────────────▶│ scanner worker (1 of 17) │
   │  (e.g. :32)  │               │   funds/<name>_worker.py │
   └──────────────┘               └──────────────────────────┘
                                                   │
                            grab(feed) ──▶ decide ──▶ upsert positions
                                                   │
                                                   ▼
                       ┌────────────────────────────────────────────┐
                       │ ~/.hermes/brain/paper_portfolio.json       │
                       │ (the shared state file — atomic writes)    │
                       │ ~/.hermes/brain/status/<worker>.json       │
                       │ (heartbeat per worker)                     │
                       └────────────────────────────────────────────┘
                                                   │
                                                   ▼
                       engine.fund_router.compute_fund_status
                                                   │
                                                   ▼
                       data/portfolio_summary.json (committed snapshot)
                                                   │
                                                   ▼
                       onchain/kite_executor.py — broadcasts marker tx
                                                   │
                                                   ▼
                       data/kite_settled.json (settlement ledger)
                                                   │
                       ┌──────────────────────────┴──────────────────┐
                       ▼                                              ▼
       scripts/reconcile.py                              scripts/watchdog.py
       (book-vs-chain integrity)                         (worker freshness)
```

## Repository layout

```
config/policy.json          ← every fund / sleeve / worker knob lives here
data/                       ← committed state: portfolio_summary, kite_settled,
                              nav_ledger, agent_registry
engine/                     ← framework — policy, risk_engine, nav_accounting,
                              backtest, fund_router, grid_base, yield_base,
                              logging_setup
funds/                      ← strategy workers (one *_worker.py per scanner)
onchain/                    ← Kite testnet executor + agent passport
scripts/                    ← ops CLIs: reconcile, watchdog, export_csv
tests/                      ← unit tests (unittest, stdlib)
docs/                       ← QUICKSTART, runbooks/, adr/
.github/workflows/ci.yml    ← lint + format + types + tests on every PR
pyproject.toml              ← deps, build, ruff/mypy/coverage config
```

## Data flow per cycle

1. **Cron fires** — single host, one-shot Python invocation. No long-lived process.
2. **Worker reads policy** — `engine.policy.sleeve_targets_for(WORKER_NAME)` returns its per-sleeve principal sizes. If `risk.engine_enabled=true`, those values pass through `engine.risk_engine.apply_engine()` first (vol-targeted / Kelly-scaled / drawdown-halted).
3. **Worker grabs the feed** — DeFiLlama, Binance, Polymarket, CoinGecko, DexScreener, Pyth Hermes, Yahoo, Stooq, Superstate. All public endpoints, no auth.
4. **Worker decides** — strategy-specific (yield accrual, grid level cross, momentum entry/exit, etc.).
5. **Worker upserts** the position in `~/.hermes/brain/paper_portfolio.json` via atomic `tmp + rename` write. Each position carries `worker`, `fund`, `sleeve` tags so the router can attribute correctly.
6. **Worker writes a status file** — `~/.hermes/brain/status/<worker>.json` with `last_heartbeat`, `position_summary`, etc. Watchdog reads this to detect stale workers.
7. **fund_router runs (separate cron tick)** — reads paper_portfolio.json, attributes positions to sleeves, writes `data/portfolio_summary.json`.
8. **kite_executor runs** — diffs sleeve content hashes against `data/kite_settled.json`, broadcasts a marker tx for any sleeve that changed, appends to the ledger. Idempotent: re-runs without changes print `nothing to settle`.
9. **NAV / reporting** — `engine.nav_accounting.compute_nav` computes per-fund unit pricing on demand. `scripts/export_csv.py` dumps reporting CSVs.

## Engine modules (framework, in `engine/`)

| Module | Responsibility |
|---|---|
| `policy` | Loads `config/policy.json` (lru_cached). Exposes `fund_cfg`, `sleeve_cfg`, `worker_cfg`, `sleeve_targets_for`, `fund_router_config`. Single entry point for every knob. |
| `risk_engine` | Optional dynamic sizing — vol-targeted + Kelly-scaled + drawdown-halted + concentration-capped. Off by default; activated by `risk.engine_enabled=true` in policy. |
| `backtest` | Synthetic Monte Carlo over the current policy + fund set. Produces Sharpe / Sortino / MaxDD / CAGR per fund at p5/p50/p95. CLI: `--show`, `--compare`, `--kelly-sweep`. |
| `nav_accounting` | Unit pricing, mgmt + perf fee accrual, hurdle-lifted HWM, monthly/quarterly/annual statement generator. Persists to `data/nav_ledger.json`. |
| `fund_router` | Reads paper_portfolio.json, attributes positions to fund sleeves via the policy mapping, writes per-fund status JSON. |
| `grid_base` | Shared engine for spot grid workers (ETH/USDC, BTC/USDC, SOL, USDC/USDT). Build levels, step cycles, resolve round-trips. Each grid worker is a thin GridConfig. |
| `yield_base` | Shared engine for fixed-yield workers. Used by superstate_uscc; older yield workers (aave, morpho, sgho, euler) keep their own implementations for stability. |
| `logging_setup` | Shared structured-JSON logger with run_id correlation. Every worker uses `setup_logger(WORKER_NAME)`. |

## Worker contract (every `funds/*_worker.py`)

```python
from engine.policy import sleeve_targets_for, worker_cfg
from engine.logging_setup import setup_logger

WORKER_NAME = "your_worker"
log = setup_logger(WORKER_NAME)

_FALLBACK_TARGETS = {"fund_x.sleeve_y": 100.0}
SLEEVE_TARGETS = sleeve_targets_for(WORKER_NAME) or _FALLBACK_TARGETS

# In run_once / cycle body:
#   1. fetch external feed (with try/except for transient failures)
#   2. for each sleeve_id, principal in SLEEVE_TARGETS.items():
#        upsert/maintain position(s) tagged
#          worker=WORKER_NAME, fund=<fund_id>, sleeve=<sleeve_id>
#   3. atomic write to ~/.hermes/brain/paper_portfolio.json
#   4. write status to ~/.hermes/brain/status/WORKER_NAME.json (must include
#      last_heartbeat — watchdog reads this)
```

## On-chain layer (Kite testnet, chain id 2368)

* `onchain/register_agent.py` — one-time DID-style agent passport registration.
  Tx data carries `kite-passport:{agent_id}:{sha256(payload)}`. Hash uses
  *compact* JSON separators `(",", ":")` — must match `scripts/reconcile.py`'s
  re-computation.
* `onchain/kite_executor.py` — reads `data/portfolio_summary.json`, computes
  per-sleeve content hash, diffs against `data/kite_settled.json["hashes"]`,
  broadcasts a self-send tx for each sleeve that changed. Tx data carries
  `hermes-kite:{agent_id}:{sleeve}:{hash}`. Idempotent.
* Settlement ledger `data/kite_settled.json` carries `txs[]` (nonce, sleeve,
  tx hash, content hash) and `hashes` (last-settled hash per sleeve).

## Operational layer

| Script | What it does | Exit code |
|---|---|---|
| `scripts/reconcile.py` | Book integrity + passport hash + on-chain nonce + per-tx existence. Web3 optional, gracefully degrades when RPC unreachable. | 0 clean / 1 drift |
| `scripts/watchdog.py` | Status file freshness + missing workers + malformed JSON. Reads `~/.hermes/brain/status/*.json`. | 0 clean / 1 alert |
| `scripts/export_csv.py` | On-demand CSV dump (funds, sleeves, positions, trades, settlements + manifest). Filters: `--fund`, `--since`. | 0 |
| `engine/nav_accounting.py --statement YYYY-MM` | Monthly/quarterly/annual NAV statement per fund. | 0 |
| `engine/risk_engine.py --show [--enable-preview]` | Per-worker sizing table — static or engine-on. | 0 |
| `engine/backtest.py [--compare \| --kelly-sweep]` | Synthetic MC distributions per fund. | 0 |

## Why this shape

* **No daemons** — operational simplicity. A worker that crashes leaves no orphan state; the next cron tick picks up where it left off. State on disk is the only authority. See `docs/adr/0001-cron-no-daemons.md`.
* **Policy-driven config** — every knob in one file, version-controlled, validated in CI. No code change required to rebalance, retune, or disable a worker. See `docs/adr/0002-policy-driven-config.md`.
* **engine/ vs funds/** — framework code lives separately from strategy code. Workers are thin (sleeve targets + fetch loop + decide). Adding a new strategy doesn't touch the framework. See `docs/adr/0003-engine-vs-funds-split.md`.
* **On-chain markers, not on-chain capital** — every fund/sleeve flip leaves a verifiable timestamp + content hash on Kite. Reconciliation script catches drift between book and chain. Real capital movement is a future swap-out of the marker tx for a real DeFi call on Kite-deployed contracts.

## Dependencies

Runtime: `web3==6.20.0`, `requests`, `python-dotenv`. Stdlib for everything else.

Dev tooling: `ruff`, `mypy`, `coverage`, `pre-commit`. Installed via `pip install -e ".[dev]"`.

CI: GitHub Actions on Python 3.11 — ruff check, ruff format check, mypy on `engine/`, json validation, coverage run + report, reconcile (book-only).
