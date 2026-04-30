#!/usr/bin/env python3
"""
aave_usdc scanner worker -- funds the stablecoin_yield / stablecoin_floor sleeves
across all three Hermes paper funds (60/40, 75/25, 90/10).

Mechanism: SUPPLY USDC on Aave V3 (Ethereum mainnet). No leverage, no impermanent
loss, no counterparty risk beyond Aave itself. Accrue interest at live supply APY
pulled from DeFiLlama (free, no key).

Why this first:
  * Simplest possible yield position (bond-equivalent, passive)
  * Funds 3 sleeves with one worker -> maximum leverage per line of code
  * Aave is the oldest/deepest lending pool -- contract risk is the floor
  * Proves the pattern: scanner worker -> status file + portfolio position

Cycle work:
  1. Fetch current USDC supply APY from DeFiLlama pool
     aa70268e-4b52-42bf-a116-608b370f9501 (Aave V3 Ethereum USDC)
  2. Accrue yield on existing paper position: principal * apy * dt_years
  3. Upsert position into ~/.hermes/brain/paper_portfolio.json tagged
     worker="aave_usdc" so fund_router attributes it correctly
  4. Emit status file at ~/.hermes/brain/status/aave_usdc.json for the
     status file + dashboard

Paper only. No real tx. No keys. R-001 compliant (free API).
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

HERMES = Path.home() / ".hermes" / "brain"
PORTFOLIO_FILE = HERMES / "paper_portfolio.json"
STATUS_FILE = HERMES / "status" / "aave_usdc.json"
LOCAL_STATE = HERMES / "aave_usdc_state.json"  # separate state for crash recovery

WORKER_NAME = "aave_usdc"
POSITION_ID = "aave_usdc_supply_eth"
SYMBOL = "AAVE_V3_USDC_ETH"
PRINCIPAL_USD = 400.00  # sized to fund_60_40_income stablecoin_yield target (40% of $1000)

POOL_ID = "aa70268e-4b52-42bf-a116-608b370f9501"
CHART_URL = f"https://yields.llama.fi/chart/{POOL_ID}"

UA = {"User-Agent": "hermes-aave-usdc/1.0"}


def fetch_apy():
    """Return (apy_decimal, source_timestamp_iso) or (None, None) on failure."""
    req = urllib.request.Request(CHART_URL, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    points = data.get("data", [])
    if not points:
        return None, None
    latest = points[-1]
    apy_pct = latest.get("apyBase") or latest.get("apy")
    if apy_pct is None:
        return None, None
    return float(apy_pct) / 100.0, str(latest.get("timestamp", ""))


def load_portfolio():
    if not PORTFOLIO_FILE.exists():
        return {
            "positions": [],
            "realized_pnl": 0.0,
            "total_trades": 0,
            "correct_trades": 0,
            "starting_capital": 10000.0,
        }
    try:
        return json.loads(PORTFOLIO_FILE.read_text())
    except Exception:
        return {"positions": [], "realized_pnl": 0.0}


def save_portfolio_atomic(pf):
    tmp = PORTFOLIO_FILE.with_suffix(".json.tmp." + str(os.getpid()))
    tmp.write_text(json.dumps(pf, indent=2))
    tmp.replace(PORTFOLIO_FILE)


def upsert_position(pf, apy):
    now = time.time()
    positions = pf.get("positions", [])
    if not isinstance(positions, list):
        positions = []
    existing = None
    for p in positions:
        if isinstance(p, dict) and p.get("worker") == WORKER_NAME and p.get("id") == POSITION_ID:
            existing = p
            break
    if existing is None:
        position = {
            "id": POSITION_ID,
            "worker": WORKER_NAME,
            "symbol": SYMBOL,
            "direction": "SUPPLY",
            "entry_price": 1.00,
            "size_usd": PRINCIPAL_USD,
            "confidence": 0.95,
            "entry_time": now,
            "horizon_hours": 8760,  # roll annually
            "resolved": False,
            "exit_price": 0.0,
            "pnl_usd": 0.0,
            "correct": False,
            "resolve_time": 0.0,
            "high_water_mark": PRINCIPAL_USD,
            "low_water_mark": PRINCIPAL_USD,
            "trailing_stop_pct": 0.0,  # yield position, no trailing stop
            "trailing_triggered": False,
            "current_apy": apy if apy is not None else 0.0,
            "last_update": now,
            "principal_usd": PRINCIPAL_USD,
        }
        positions.append(position)
        existing = position
    else:
        last = existing.get("last_update", existing.get("entry_time", now))
        if apy is not None and now > last:
            dt_years = (now - last) / (365.25 * 24 * 3600)
            accrued = existing.get("size_usd", PRINCIPAL_USD) * apy * dt_years
            existing["size_usd"] = round(existing.get("size_usd", PRINCIPAL_USD) + accrued, 6)
            existing["pnl_usd"] = round(existing["size_usd"] - PRINCIPAL_USD, 6)
            existing["high_water_mark"] = max(
                existing.get("high_water_mark", PRINCIPAL_USD), existing["size_usd"]
            )
            existing["low_water_mark"] = min(
                existing.get("low_water_mark", PRINCIPAL_USD), existing["size_usd"]
            )
        existing["current_apy"] = apy if apy is not None else existing.get("current_apy", 0.0)
        existing["last_update"] = now
        if "principal_usd" not in existing:
            existing["principal_usd"] = PRINCIPAL_USD

    pf["positions"] = positions
    return existing


def write_status(position, apy, ok, error_msg=None):
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(UTC).astimezone().isoformat()
    size_usd = position.get("size_usd", 0) if position else 0
    pnl = position.get("pnl_usd", 0) if position else 0
    status = {
        "worker_name": WORKER_NAME,
        "strategy_type": "stablecoin_yield",
        "status": "active" if ok else "degraded",
        "last_heartbeat": now_iso,
        "cycle_count": 1,
        "position_summary": {
            "open_positions": 1 if position else 0,
            "closed_positions": 0,
            "total_capital_deployed_usd": round(size_usd, 4),
            "realized_pnl_usd": 0.0,
            "unrealized_pnl_usd": round(pnl, 6),
        },
        "performance": {
            "current_apy_pct": round((apy or 0) * 100, 4),
            "pnl_all_time": round(pnl, 6),
            "principal_usd": PRINCIPAL_USD,
            "win_rate": None,
            "trades_last_24h": 0,
        },
        "risk": {
            "position_sizing_method": "sleeve_target_usd",
            "stop_loss_pct": None,
            "counterparty": "aave_v3_ethereum_mainnet",
            "protocol_risk": "smart_contract_v3",
        },
        "strategy_config": {
            "protocol": "aave-v3",
            "chain": "ethereum",
            "asset": "USDC",
            "pool_id": POOL_ID,
            "data_source": "defillama_free",
            "paper_principal_usd": PRINCIPAL_USD,
            "fund_sleeves": [
                "fund_60_40_income.stablecoin_yield",
                "fund_75_25_balanced.stablecoin_yield",
                "fund_90_10_growth.stablecoin_floor",
            ],
        },
        "errors_last_24h": 0 if ok else 1,
        "health_check": "green" if ok else "yellow",
    }
    if error_msg:
        status["last_error"] = error_msg
    STATUS_FILE.write_text(json.dumps(status, indent=2))


def main():
    apy = None
    ok = True
    err = None
    try:
        apy, ts = fetch_apy()
        if apy is None:
            ok = False
            err = "APY endpoint returned no data"
    except Exception as e:
        ok = False
        err = f"fetch failed: {e}"

    pf = load_portfolio()
    position = upsert_position(pf, apy)
    save_portfolio_atomic(pf)
    write_status(position, apy, ok, err)

    apy_str = f"{apy * 100:.4f}%" if apy is not None else "UNK"
    size_str = f"{position.get('size_usd', 0):.6f}" if position else "0"
    pnl_str = f"{position.get('pnl_usd', 0):.6f}" if position else "0"
    print(f"[aave_usdc] apy={apy_str}  size_usd=${size_str}  pnl=${pnl_str}  ok={ok}")


if __name__ == "__main__":
    main()
