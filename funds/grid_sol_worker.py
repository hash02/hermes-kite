#!/usr/bin/env python3
"""
grid_sol -- spot grid trader on SOL/USDC (Binance public spot).

Covers aggressive_grid (90/10 only — SOL vol fits the growth fund risk
budget). Thin config on grid_base.py; policy-driven.
"""

from __future__ import annotations

from engine.grid_base import GridConfig, run_grid
from engine.policy import sleeve_targets_for, worker_cfg

WORKER_NAME = "grid_sol"

_FALLBACK_TARGETS = {"fund_90_10_growth.aggressive_grid": 66.00}
_cfg = worker_cfg(WORKER_NAME)

CONFIG = GridConfig(
    worker_name=WORKER_NAME,
    symbol="SOLUSDC",
    price_url="https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDC",
    klines_url="https://api.binance.com/api/v3/klines?symbol=SOLUSDC&interval=1h&limit=24",
    sleeve_targets=sleeve_targets_for(WORKER_NAME) or _FALLBACK_TARGETS,
    grid_half=_cfg.get("grid_half", 5),
    grid_band_pct=_cfg.get("band_pct", 0.08),
)


if __name__ == "__main__":
    run_grid(CONFIG)
