#!/usr/bin/env python3
"""
grid_sol -- spot grid trader on SOL/USDC (Binance public spot).

Covers aggressive_grid (90/10 only — SOL vol fits the growth fund risk
budget; income + balanced funds stick to ETH/BTC grids).

Shared engine in grid_base.py; this file is a thin config.
"""
from __future__ import annotations
from grid_base import GridConfig, run_grid

CONFIG = GridConfig(
    worker_name="grid_sol",
    symbol="SOLUSDC",
    price_url="https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDC",
    klines_url="https://api.binance.com/api/v3/klines?symbol=SOLUSDC&interval=1h&limit=24",
    sleeve_targets={
        "fund_90_10_growth.aggressive_grid": 66.00,  # SOL leg of 3-way $200 split
    },
    grid_half=5,
    grid_band_pct=0.08,   # ±8% — SOL oscillates wider than ETH/BTC
)


if __name__ == "__main__":
    run_grid(CONFIG)
