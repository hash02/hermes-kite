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

## Future: risk engine

When `risk.engine_enabled=true`, the (not-yet-shipped) `funds/risk_engine.py`
module will derive `principal_usd` dynamically from target portfolio vol +
realized per-sleeve vol + Kelly fraction, instead of the static values in
this file. The static values then serve as "boot config" (used at cold start
before enough return history exists to estimate vol).
