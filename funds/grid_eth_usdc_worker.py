#!/usr/bin/env python3
"""
grid_eth_usdc -- spot grid trader on ETH/USDC (Binance public spot).

Covers structural_grid / aggressive_grid sleeves across all three funds.
Shared engine lives in grid_base.py; this file is a thin config.

Paper only. R-001 compliant (Binance public endpoints, no key).
"""
from __future__ import annotations
from grid_base import GridConfig, run_grid

CONFIG = GridConfig(
    worker_name="grid_eth_usdc",
    symbol="ETHUSDC",
    price_url="https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDC",
    klines_url="https://api.binance.com/api/v3/klines?symbol=ETHUSDC&interval=1h&limit=24",
    sleeve_targets={
        # 60/40 structural_grid $250 split with grid_stables
        "fund_60_40_income.structural_grid": 125.00,
        # 75/25 structural_grid $250 split with grid_btc_usdc
        "fund_75_25_balanced.structural_grid": 125.00,
        # 90/10 aggressive_grid $200 split with grid_btc_usdc + grid_sol
        "fund_90_10_growth.aggressive_grid": 67.00,
    },
    grid_half=5,
    grid_band_pct=0.06,   # ±6% band (ETH spot tends to oscillate cleanly here)
)


if __name__ == "__main__":
    run_grid(CONFIG)
