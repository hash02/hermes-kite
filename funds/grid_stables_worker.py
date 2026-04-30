#!/usr/bin/env python3
"""
grid_stables -- spot grid trader on USDC/USDT (Binance public spot).

Stablecoin pair grid — mean-reverts around $1.0000. Tiny band (±0.3%),
fills on micro-oscillations. Covers structural_grid (60/40) only.
Thin config on grid_base.py; policy-driven.
"""

from __future__ import annotations

from grid_base import GridConfig, run_grid
from policy import sleeve_targets_for, worker_cfg

WORKER_NAME = "grid_stables"

_FALLBACK_TARGETS = {"fund_60_40_income.structural_grid": 125.00}
_cfg = worker_cfg(WORKER_NAME)

CONFIG = GridConfig(
    worker_name=WORKER_NAME,
    symbol="USDCUSDT",
    price_url="https://api.binance.com/api/v3/ticker/price?symbol=USDCUSDT",
    klines_url="https://api.binance.com/api/v3/klines?symbol=USDCUSDT&interval=1h&limit=24",
    sleeve_targets=sleeve_targets_for(WORKER_NAME) or _FALLBACK_TARGETS,
    grid_half=_cfg.get("grid_half", 5),
    grid_band_pct=_cfg.get("band_pct", 0.003),
)


if __name__ == "__main__":
    run_grid(CONFIG)
