#!/usr/bin/env python3
"""
grid_eth_usdc -- spot grid trader on ETH/USDC (Binance public spot).

Covers structural_grid / aggressive_grid sleeves across all three funds.
Shared engine lives in grid_base.py; this file is a thin config. Sleeve
sizes + grid band come from config/policy.json; fallback defaults below.

Paper only. R-001 compliant (Binance public endpoints, no key).
"""

from __future__ import annotations

from engine.grid_base import GridConfig, run_grid
from engine.policy import sleeve_targets_for, worker_cfg

WORKER_NAME = "grid_eth_usdc"

_FALLBACK_TARGETS = {
    "fund_60_40_income.structural_grid": 125.00,
    "fund_75_25_balanced.structural_grid": 125.00,
    "fund_90_10_growth.aggressive_grid": 67.00,
}
_cfg = worker_cfg(WORKER_NAME)

CONFIG = GridConfig(
    worker_name=WORKER_NAME,
    symbol="ETHUSDC",
    price_url="https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDC",
    klines_url="https://api.binance.com/api/v3/klines?symbol=ETHUSDC&interval=1h&limit=24",
    sleeve_targets=sleeve_targets_for(WORKER_NAME) or _FALLBACK_TARGETS,
    grid_half=_cfg.get("grid_half", 5),
    grid_band_pct=_cfg.get("band_pct", 0.06),
)


if __name__ == "__main__":
    run_grid(CONFIG)
