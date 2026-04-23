#!/usr/bin/env python3
"""
Fund Router — maps Hermes paper positions to three fund profiles.

Each fund has a target allocation across sleeves. This router reads the live
paper_portfolio.json, attributes positions to their fund sleeves via a worker
mapping, and writes per-fund status files the dashboard can render.

Usage:
    python3 fund_router.py                  # one-shot status write
    python3 fund_router.py --verbose        # print per-fund detail
    python3 fund_router.py --capital 10000  # change assumed fund capital

Outputs (written to ~/.hermes/brain/funds/):
    fund_60_40.json, fund_75_25.json, fund_90_10.json

Reads:
    ~/.hermes/brain/paper_portfolio.json
"""
from __future__ import annotations
import argparse
import json
import os
import time
from collections import defaultdict
from pathlib import Path

HOME = Path.home()
PORTFOLIO = HOME / ".hermes" / "brain" / "paper_portfolio.json"
FUND_DIR = HOME / ".hermes" / "brain" / "funds"

# Fund definitions. Each sleeve has a target percent of fund capital and a
# list of workers whose positions feed that sleeve. "cash" sleeve is residual.
FUND_CONFIG = {
    "fund_60_40_income": {
        "name": "FUND 60/40 — Hermes Steady Monthly Income",
        "target_annual_return_pct": 7.4,
        "max_drawdown_pct": 5.0,
        "payout_cadence": "monthly",
        "sleeves": {
            "stablecoin_yield":    {"target_pct": 40, "workers": ["aave_usdc", "morpho_usdc", "euler_pyusd"]},
            "delta_neutral":       {"target_pct": 25, "workers": ["delta_neutral_funding"]},
            "structural_grid":     {"target_pct": 25, "workers": ["grid_eth_usdc", "grid_stables"]},
            "cash":                {"target_pct": 10, "workers": []},
        },
    },
    "fund_75_25_balanced": {
        "name": "FUND 75/25 — Hermes Quarterly Balanced",
        "target_annual_return_pct": 11.1,
        "max_drawdown_pct": 10.0,
        "payout_cadence": "quarterly",
        "sleeves": {
            "stablecoin_yield":    {"target_pct": 25, "workers": ["aave_usdc", "sgho", "superstate_uscc"]},
            "delta_neutral":       {"target_pct": 20, "workers": ["delta_neutral_funding"]},
            "structural_grid":     {"target_pct": 25, "workers": ["grid_eth_usdc", "grid_btc_usdc"]},
            "directional":         {"target_pct": 20, "workers": ["pyth_momentum", "polymarket_btc_updown"]},
            "tokenized_stocks":    {"target_pct": 5,  "workers": ["xstocks_grid"]},
            "cash":                {"target_pct": 5,  "workers": []},
        },
    },
    "fund_90_10_growth": {
        "name": "FUND 90/10 — Hermes Annual Growth Machine",
        "target_annual_return_pct": 34.0,
        "max_drawdown_pct": 25.0,
        "payout_cadence": "annual",
        "sleeves": {
            "stablecoin_floor":    {"target_pct": 10, "workers": ["aave_usdc", "sgho"]},
            "latency_arb":         {"target_pct": 30, "workers": ["polymarket_btc_updown"]},
            "aggressive_grid":     {"target_pct": 20, "workers": ["grid_eth_usdc", "grid_btc_usdc", "grid_sol"]},
            "directional_momentum":{"target_pct": 20, "workers": ["tv_momentum"]},
            "memecoin_sniper":     {"target_pct": 10, "workers": ["crypto_memecoins", "wow_sniper_base"]},
            "xstocks_directional": {"target_pct": 10, "workers": ["xstocks_directional"]},
        },
    },
}


def load_portfolio() -> list:
    if not PORTFOLIO.exists():
        return []
    d = json.loads(PORTFOLIO.read_text())
    return d if isinstance(d, list) else d.get("positions", [])


def compute_fund_status(fund_id: str, fund_cfg: dict, positions: list, capital: float) -> dict:
    """Build the status dict for one fund."""
    worker_to_sleeve = {}
    for sleeve_id, sleeve in fund_cfg["sleeves"].items():
        for w in sleeve["workers"]:
            worker_to_sleeve[w] = sleeve_id

    sleeve_agg = defaultdict(lambda: {
        "positions": 0, "resolved": 0, "wins": 0,
        "pnl_usd": 0.0, "staked_usd": 0.0, "open_exposure_usd": 0.0,
    })

    for p in positions:
        w = p.get("worker", "")
        if w not in worker_to_sleeve:
            continue
        # If a position is fund-scoped (workers that ship per-sleeve sizing tag
        # positions with `fund`), only attribute it to its own fund. Legacy
        # untagged positions still attribute to every fund that lists the worker.
        pos_fund = p.get("fund")
        if pos_fund and pos_fund != fund_id:
            continue
        sleeve_id = worker_to_sleeve[w]
        s = sleeve_agg[sleeve_id]
        s["positions"] += 1
        s["staked_usd"] += p.get("size_usd", 0)
        if p.get("resolved"):
            s["resolved"] += 1
            if p.get("correct"):
                s["wins"] += 1
            s["pnl_usd"] += p.get("pnl_usd", 0)
        else:
            s["open_exposure_usd"] += p.get("size_usd", 0)

    # Compose sleeve output with drift + funding status
    sleeves_out = {}
    total_pnl = 0.0
    total_resolved = 0
    total_wins = 0
    total_staked = 0.0
    funded_sleeves = 0
    for sleeve_id, sleeve_cfg in fund_cfg["sleeves"].items():
        s = sleeve_agg[sleeve_id]
        target_usd = capital * sleeve_cfg["target_pct"] / 100
        open_usd = s["open_exposure_usd"]
        drift_pct = ((open_usd - target_usd) / target_usd * 100) if target_usd > 0 else 0
        win_rate = (s["wins"] / s["resolved"] * 100) if s["resolved"] > 0 else 0.0
        funded = s["positions"] > 0 or sleeve_id == "cash"
        if funded:
            funded_sleeves += 1
        sleeves_out[sleeve_id] = {
            "target_pct": sleeve_cfg["target_pct"],
            "target_usd": round(target_usd, 2),
            "open_exposure_usd": round(open_usd, 2),
            "drift_pct": round(drift_pct, 1),
            "workers_configured": sleeve_cfg["workers"],
            "positions_total": s["positions"],
            "resolved": s["resolved"],
            "win_rate_pct": round(win_rate, 1),
            "pnl_usd": round(s["pnl_usd"], 2),
            "staked_usd": round(s["staked_usd"], 2),
            "funded": funded,
        }
        total_pnl += s["pnl_usd"]
        total_resolved += s["resolved"]
        total_wins += s["wins"]
        total_staked += s["staked_usd"]

    overall_wr = (total_wins / total_resolved * 100) if total_resolved > 0 else 0.0
    sleeve_count = len(fund_cfg["sleeves"])
    coverage_pct = funded_sleeves / sleeve_count * 100

    return {
        "fund_id": fund_id,
        "name": fund_cfg["name"],
        "as_of": int(time.time()),
        "capital_usd": capital,
        "target_annual_return_pct": fund_cfg["target_annual_return_pct"],
        "max_drawdown_pct": fund_cfg["max_drawdown_pct"],
        "payout_cadence": fund_cfg["payout_cadence"],
        "coverage_pct": round(coverage_pct, 1),
        "overall_win_rate_pct": round(overall_wr, 1),
        "total_trades": sum(s["positions"] for s in sleeve_agg.values()),
        "total_resolved": total_resolved,
        "total_pnl_usd": round(total_pnl, 2),
        "total_staked_usd": round(total_staked, 2),
        "sleeves": sleeves_out,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=1000.0, help="Assumed per-fund capital (paper)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    FUND_DIR.mkdir(parents=True, exist_ok=True)
    positions = load_portfolio()
    print(f"[fund_router] portfolio positions loaded: {len(positions)}")

    for fund_id, fund_cfg in FUND_CONFIG.items():
        status = compute_fund_status(fund_id, fund_cfg, positions, args.capital)
        out = FUND_DIR / f"{fund_id}.json"
        out.write_text(json.dumps(status, indent=2))
        if args.verbose:
            print(f"\n=== {status['name']} ===")
            print(f"  coverage: {status['coverage_pct']}% of sleeves funded")
            print(f"  trades: {status['total_trades']}  resolved: {status['total_resolved']}  WR: {status['overall_win_rate_pct']}%  PnL: ${status['total_pnl_usd']:+.2f}")
            for sid, s in status["sleeves"].items():
                marker = " " if s["funded"] else "!"
                print(f"  {marker} {sid:22s} tgt {s['target_pct']:>2}% (${s['target_usd']:.0f})  live ${s['open_exposure_usd']:.2f}  drift {s['drift_pct']:+.1f}%  trades {s['positions_total']}  PnL ${s['pnl_usd']:+.2f}")
        else:
            print(f"  wrote {out.name}  coverage={status['coverage_pct']}%  PnL=${status['total_pnl_usd']:+.2f}")


if __name__ == "__main__":
    main()
