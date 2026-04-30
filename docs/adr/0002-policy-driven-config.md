# ADR 0002 — Policy-driven config (`config/policy.json`)

* **Status**: Accepted
* **Date**: 2026-04-23
* **Context**: Worker tunables (per-sleeve principal, entry thresholds, EMA windows, grid bands, fee schedules) were originally hardcoded as Python constants — one set per worker, scattered across 17 files.

## Decision

Every knob lives in `config/policy.json`. Workers read via `engine.policy.sleeve_targets_for(WORKER_NAME)` and `engine.policy.worker_cfg(WORKER_NAME)` instead of hardcoded constants. Each worker keeps a `_FALLBACK_TARGETS` dict so it remains runnable when the policy file is missing or malformed.

## Considered alternatives

1. **Keep constants in Python.** Status quo before this change. Editing a sleeve target meant a code change + PR + redeploy.
2. **YAML config.** Nicer to write than JSON, supports comments. Adds a dep (`pyyaml`); diverges from the rest of our state files which are all JSON; CI's existing JSON validation hook doesn't cover it.
3. **Per-fund TOML files.** Splits config across files keyed by fund. Cleaner ownership story but harder to see the whole allocation at once.
4. **Database (SQLite).** Schema enforcement, transactional updates, queryable history. Overkill for a config that changes maybe weekly.

## Why JSON

* **Stdlib-only parser.** No new dep; every existing tool already touches JSON.
* **CI hook for free.** `python -m json.tool config/policy.json > /dev/null` catches malformed commits in `.github/workflows/ci.yml`.
* **Trivial diff in PRs.** Reviewers see exactly what changed.
* **Same shape as the data layer.** `data/portfolio_summary.json`, `data/nav_ledger.json`, `data/kite_settled.json` are all JSON. One mental model.

## Lazy-import to break cycles

`engine.policy.sleeve_targets_for()` routes through `engine.risk_engine.apply_engine()` only when `risk.engine_enabled=true`. The import is inside the function body, not at module load — `engine.risk_engine` imports `engine.policy._WORKER_META` indirectly, so a top-level import here would create a cycle.

## Consequences

* The policy file is the single point of failure for worker sizing. Mitigated by:
  - JSON validation in CI on every PR.
  - Workers carry `_FALLBACK_TARGETS` so they keep running with built-in defaults if the file is missing.
  - `engine.policy._load_policy()` is `@lru_cache(maxsize=1)` — read once per process, cached after. Tests call `policy.reload()` to drop the cache.
* Editing requires cron-tick latency to take effect (workers re-read on the next invocation). Acceptable.
* Adding a new worker now requires *three* edits: `funds/<new>_worker.py`, `engine.risk_engine._WORKER_META`, `config/policy.json`. Documented in `CONTRIBUTING.md`.

## Risk-engine knobs live in the same file

The `risk` block (`kelly_fraction`, `target_portfolio_vol_pct`, etc.) is in the same JSON. Splitting it off was considered but rejected — the engine knobs and the per-sleeve principals are read together; co-locating keeps the "what does this fund deploy?" question answerable from one file.
