#!/usr/bin/env python3
"""
sgho scanner worker -- diversifies the 75/25 stablecoin_yield and 90/10
stablecoin_floor sleeves alongside aave_usdc.

Mechanism: HOLD sGHO (Aave's GHO Savings Module). sGHO is an ERC-4626 vault
that pays the GHO Savings Rate set by Aave governance. Different exposure
profile to USDC supply (GHO is Aave-issued stable backed by overcollateralized
crypto), so pairs well with aave_usdc to cut single-asset risk.

Pool: sGHO (Aave Savings GHO) on DeFiLlama
  pool_id: 4cda6dc8-2cae-4cd6-9889-aab3eb9bb6b9

Cycle work:
  1. Fetch APY for sGHO from DeFiLlama
  2. Upsert one fund-scoped paper position per sleeve in SLEEVE_TARGETS
  3. Accrue principal * apy * dt_years on each
  4. Emit status

Same contract as aave_usdc and morpho_usdc:
  - Tags positions with worker="sgho", fund=<fund_id>, sleeve=<sleeve_id>
  - Upserts into ~/.hermes/brain/paper_portfolio.json
  - Status at ~/.hermes/brain/status/sgho.json

Paper only. R-001 compliant.
"""
from __future__ import annotations
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERMES = Path.home() / ".hermes" / "brain"
PORTFOLIO_FILE = HERMES / "paper_portfolio.json"
STATUS_FILE = HERMES / "status" / "sgho.json"

WORKER_NAME = "sgho"
SYMBOL = "AAVE_SGHO"

# 75/25 stablecoin_yield target $250 (sharing with aave + superstate, third
# pending) and 90/10 stablecoin_floor target $100 (sharing with aave).
SLEEVE_TARGETS = {
    "fund_75_25_balanced.stablecoin_yield": 125.00,
    "fund_90_10_growth.stablecoin_floor": 50.00,
}

POOL_ID = "4cda6dc8-2cae-4cd6-9889-aab3eb9bb6b9"
CHART_URL = f"https://yields.llama.fi/chart/{POOL_ID}"

UA = {"User-Agent": "hermes-sgho/1.0"}


def fetch_apy():
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
        return {"positions": [], "realized_pnl": 0.0, "total_trades": 0,
                "correct_trades": 0, "starting_capital": 10000.0}
    try:
        return json.loads(PORTFOLIO_FILE.read_text())
    except Exception:
        return {"positions": [], "realized_pnl": 0.0}


def save_portfolio_atomic(pf):
    tmp = PORTFOLIO_FILE.with_suffix(".json.tmp." + str(os.getpid()))
    tmp.write_text(json.dumps(pf, indent=2))
    tmp.replace(PORTFOLIO_FILE)


def position_id(sleeve_id: str) -> str:
    return f"sgho:{sleeve_id}"


def upsert_sleeve_position(pf, sleeve_id: str, principal: float, apy):
    now = time.time()
    fund_id = sleeve_id.split(".", 1)[0]
    pos_id = position_id(sleeve_id)
    positions = pf.get("positions", [])
    if not isinstance(positions, list):
        positions = []
    existing = None
    for p in positions:
        if isinstance(p, dict) and p.get("worker") == WORKER_NAME and p.get("id") == pos_id:
            existing = p
            break
    if existing is None:
        position = {
            "id": pos_id,
            "worker": WORKER_NAME,
            "fund": fund_id,
            "sleeve": sleeve_id,
            "symbol": SYMBOL,
            "direction": "HOLD",
            "entry_price": 1.00,
            "size_usd": principal,
            "principal_usd": principal,
            "confidence": 0.93,
            "entry_time": now,
            "horizon_hours": 8760,
            "resolved": False,
            "exit_price": 0.0,
            "pnl_usd": 0.0,
            "correct": False,
            "resolve_time": 0.0,
            "high_water_mark": principal,
            "low_water_mark": principal,
            "trailing_stop_pct": 0.0,
            "trailing_triggered": False,
            "current_apy": apy if apy is not None else 0.0,
            "last_update": now,
        }
        positions.append(position)
        existing = position
    else:
        last = existing.get("last_update", existing.get("entry_time", now))
        prior_principal = existing.get("principal_usd", principal)
        if prior_principal != principal:
            accrued_so_far = existing.get("size_usd", prior_principal) - prior_principal
            existing["principal_usd"] = principal
            existing["size_usd"] = principal + accrued_so_far
        if apy is not None and now > last:
            dt_years = (now - last) / (365.25 * 24 * 3600)
            accrued = existing.get("size_usd", principal) * apy * dt_years
            existing["size_usd"] = round(existing.get("size_usd", principal) + accrued, 6)
            existing["pnl_usd"] = round(existing["size_usd"] - existing["principal_usd"], 6)
            existing["high_water_mark"] = max(existing.get("high_water_mark", principal), existing["size_usd"])
            existing["low_water_mark"] = min(existing.get("low_water_mark", principal), existing["size_usd"])
        existing["current_apy"] = apy if apy is not None else existing.get("current_apy", 0.0)
        existing["last_update"] = now
        existing["fund"] = fund_id
        existing["sleeve"] = sleeve_id

    pf["positions"] = positions
    return existing


def write_status(positions, apy, ok, error_msg=None):
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).astimezone().isoformat()
    total_size = sum(p.get("size_usd", 0) for p in positions)
    total_pnl = sum(p.get("pnl_usd", 0) for p in positions)
    total_principal = sum(p.get("principal_usd", 0) for p in positions)
    status = {
        "worker_name": WORKER_NAME,
        "strategy_type": "stablecoin_yield",
        "status": "active" if ok else "degraded",
        "last_heartbeat": now_iso,
        "cycle_count": 1,
        "position_summary": {
            "open_positions": len(positions),
            "closed_positions": 0,
            "total_capital_deployed_usd": round(total_size, 4),
            "realized_pnl_usd": 0.0,
            "unrealized_pnl_usd": round(total_pnl, 6),
        },
        "performance": {
            "current_apy_pct": round((apy or 0) * 100, 4),
            "pnl_all_time": round(total_pnl, 6),
            "principal_usd": round(total_principal, 2),
            "win_rate": None,
            "trades_last_24h": 0,
        },
        "risk": {
            "position_sizing_method": "per_sleeve_target",
            "stop_loss_pct": None,
            "counterparty": "aave_savings_gho",
            "protocol_risk": "smart_contract_aave_savings",
        },
        "strategy_config": {
            "protocol": "aave-savings-gho",
            "chain": "ethereum",
            "asset": "sGHO",
            "pool_id": POOL_ID,
            "data_source": "defillama_free",
            "sleeve_targets_usd": SLEEVE_TARGETS,
            "fund_sleeves": list(SLEEVE_TARGETS.keys()),
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
        apy, _ = fetch_apy()
        if apy is None:
            ok = False
            err = "APY endpoint returned no data"
    except Exception as e:
        ok = False
        err = f"fetch failed: {e}"

    pf = load_portfolio()
    positions = []
    for sleeve_id, principal in SLEEVE_TARGETS.items():
        positions.append(upsert_sleeve_position(pf, sleeve_id, principal, apy))
    save_portfolio_atomic(pf)
    write_status(positions, apy, ok, err)

    apy_str = f"{apy*100:.4f}%" if apy is not None else "UNK"
    total_size = sum(p.get("size_usd", 0) for p in positions)
    total_pnl = sum(p.get("pnl_usd", 0) for p in positions)
    print(f"[sgho] apy={apy_str}  sleeves={len(positions)}  "
          f"total_size=${total_size:.6f}  total_pnl=${total_pnl:.6f}  ok={ok}")


if __name__ == "__main__":
    main()
