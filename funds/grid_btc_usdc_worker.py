#!/usr/bin/env python3
"""
grid_btc_usdc -- spot grid trader on BTC/USDC (Binance public spot).

Covers structural_grid (75/25) + aggressive_grid (90/10). Shares 60/40's
structural budget with grid_eth_usdc (both feed 60_40.structural_grid only
via ETH; BTC variant is 75/25-and-up).

Shared engine in grid_base.py; this file is a thin config.
"""
from __future__ import annotations
from grid_base import GridConfig, run_grid

CONFIG = GridConfig(
    worker_name="grid_btc_usdc",
    symbol="BTCUSDC",
    price_url="https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDC",
    klines_url="https://api.binance.com/api/v3/klines?symbol=BTCUSDC&interval=1h&limit=24",
    sleeve_targets={
        "fund_75_25_balanced.structural_grid": 125.00,  # pairs with grid_eth_usdc at $125
        "fund_90_10_growth.aggressive_grid": 67.00,     # 3-way split with eth + sol
    },
    grid_half=5,
    grid_band_pct=0.05,   # ±5% — BTC spot is slightly less volatile than ETH
)


if __name__ == "__main__":
    run_grid(CONFIG)
