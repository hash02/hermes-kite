# Policy config

`policy.json` is the single source of truth for every knob that used to be
hardcoded across the worker fleet. Workers load it at cycle start via
`funds/policy.py`; if the file is missing, workers fall back to their
built-in defaults (safe to delete).

## Structure

```
funds:
  <fund_id>:                     # e.g. fund_60_40_income
    name:                        # display string
    capital_usd:                 # assumed per-fund paper capital
    target_annual_return_pct:
    max_drawdown_pct:
    payout_cadence:              # monthly / quarterly / annual
    sleeves:
      <sleeve_id>:               # e.g. stablecoin_yield
        target_pct:              # % of fund capital
        workers:
          <worker_name>:
            principal_usd:               # static supply/hold size
            target_deployment_usd:       # total sleeve deployment this worker aims for
            principal_usd_per_symbol:    # per-symbol basket size

workers:
  <worker_name>:                 # per-worker gating / thresholds
    # e.g. min_annualized_rate_pct, entry_momentum_pct, grid_half, band_pct

risk:                            # math-engine knobs (engine_enabled=false today)
  engine_enabled:
  kelly_fraction:
  target_portfolio_vol_pct:
  max_concentration_per_counterparty_pct:
  max_drawdown_halt_per_fund_pct:
```

## How workers read it

Every worker calls `from policy import sleeve_targets_for, worker_cfg` and
replaces its static `SLEEVE_TARGETS` dict + local constants with lookups:

```python
SLEEVE_TARGETS = sleeve_targets_for("aave_usdc")     # {sleeve_id: principal_usd}
_cfg = worker_cfg("delta_neutral_funding")
MIN_ANNUALIZED_RATE = _cfg.get("min_annualized_rate_pct", 8.0)
```

`fund_router.py` reads `funds` + `sleeves` the same way for allocation
routing.

## Editing

1. Edit `config/policy.json` — no code changes required.
2. Re-run any worker; it'll pick up the new values next cycle.
3. For layout/sanity checks: `python3 -m json.tool config/policy.json`.

## Fees (per-fund)

Each fund has a `fees` block that drives `funds/nav_accounting.py`:

- `management_fee_annual_pct` — linear daily accrual on gross equity.
- `performance_fee_pct` — charged on NAV above the (hurdle-lifted) HWM.
- `hurdle_rate_annual_pct` — annualized hurdle: perf fee only applies to
  returns above HWM × (1 + hurdle × days_since_crystallization / 365).
- `mgmt_fee_crystallization_cadence` — monthly / quarterly / annual.
- `perf_fee_crystallization_cadence` — monthly / quarterly / annual.

Current defaults escalate with risk: 60/40 = 1% / 10% / 0%; 75/25 = 1.5% /
15% / 4%; 90/10 = 2% / 20% / 8%. Crystallization moves accrued → paid and
(for perf) resets HWM to post-fee NAV.

## Risk engine

The `risk` block drives `funds/risk_engine.py`. When `engine_enabled=true`,
every `sleeve_targets_for(worker)` call routes through `risk_engine.apply_engine()`
which returns vol-targeted / Kelly-scaled / drawdown-halted sizes instead of
the raw JSON values. When disabled, static values pass through. Knobs:

- `kelly_fraction` — final scalar on every size (default 0.25 = quarter-Kelly).
- `target_portfolio_vol_pct` — portfolio-level annualized vol target. Each
  sleeve gets `target / sqrt(n_sleeves_in_fund)` as its own vol budget.
- `max_concentration_per_counterparty_pct` — pro-rata scales down sizes on
  any counterparty exceeding this % of fund capital.
- `max_drawdown_halt_per_fund_pct` — if fund drawdown ≤ -this, every sleeve
  in the fund sizes to zero. Set `null` to disable.

Preview the engine's effect without flipping the flag:

```bash
python3 funds/risk_engine.py --show --enable-preview
```
