#!/usr/bin/env python3
"""
grid_stables -- spot grid trader on USDC/USDT (Binance public spot).

Stablecoin pair grid — mean-reverts around $1.0000. Tiny band (±0.3%),
fills on micro-oscillations. Covers structural_grid (60/40) only — the
most conservative grid exposure, zero directional risk.

Shared engine in grid_base.py; this file is a thin config.
"""
from __future__ import annotations
from grid_base import GridConfig, run_grid

CONFIG = GridConfig(
    worker_name="grid_stables",
    symbol="USDCUSDT",
    price_url="https://api.binance.com/api/v3/ticker/price?symbol=USDCUSDT",
    klines_url="https://api.binance.com/api/v3/klines?symbol=USDCUSDT&interval=1h&limit=24",
    sleeve_targets={
        # 60/40 structural_grid splits: grid_eth_usdc $125 + grid_stables $125 = $250
        "fund_60_40_income.structural_grid": 125.00,
    },
    grid_half=5,
    grid_band_pct=0.003,  # ±0.3% — stablecoin pair sits inside this almost always
)


if __name__ == "__main__":
    run_grid(CONFIG)
