#!/usr/bin/env python3
"""
Delta-Neutral Funding Rate Arbitrage Worker

Mechanism: SHORT PERP + LONG SPOT = net-zero directional exposure.
Revenue: funding payments only. Risk: liquidation, sign-flip, exchange risk.

Paper-mode worker that funds two sleeves:
  - fund_60_40_income.delta_neutral  (25% = $250 target)
  - fund_75_25_balanced.delta_neutral (20% = $200 target)

Cycle work (default mode, no args):
  1. Pull live Binance funding rates (public endpoint, no auth)
  2. Rank by |annualized rate|, threshold MIN_ANNUALIZED_RATE
  3. Accrue paper funding on existing open positions:
     size_usd += notional * funding_rate * cycles_elapsed
  4. Unwind (resolve) any position whose current funding sign flipped from entry
     sign (UNWIND_ON_SIGN_FLIP behaviour)
  5. If we're below MAX_OPEN_POSITIONS and top opps qualify, open new paper
     positions tagged worker="delta_neutral_funding" so fund_router attributes
     them to both delta_neutral sleeves
  6. Emit status file + upsert into paper_portfolio.json

Scan-only mode:
  python3 delta_neutral_worker.py --scan
  python3 delta_neutral_worker.py --scan --min-rate 10

Paper only. No real tx. No exchange keys. R-001 compliant (free Binance public).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# --- Config (mirrors funds/DELTA_NEUTRAL_WORKER_PROPOSAL.md) ---
MIN_ANNUALIZED_RATE = 8.0  # % annualized, absolute
MAX_POSITION_USD = 50.0
MAX_OPEN_POSITIONS = 2
MAX_TOTAL_DEPLOYED = 400.0
MIN_FUNDING_HISTORY_HOURS = 24  # 3 cycles of same-sign history
UNWIND_ON_SIGN_FLIP = True
# In paper mode, allow a position on the first sighting of a qualifying |rate|
# without waiting for same-sign confirmation. Live mode must flip this off.
PAPER_MODE_RELAXED_GATE = True

BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"

WORKER_NAME = "delta_neutral_funding"

HOME = Path.home()
HERMES = HOME / ".hermes" / "brain"
STATE_FILE = HERMES / "delta_neutral_state.json"
PORTFOLIO_FILE = HERMES / "paper_portfolio.json"
STATUS_FILE = HERMES / "status" / "delta_neutral_funding.json"

UA = {"User-Agent": "hermes-delta-neutral/1.0"}


class FundingOpp(NamedTuple):
    symbol: str
    mark_price: float
    funding_rate: float  # per-cycle (8h)
    annualized_pct: float  # rate * 3 * 365 * 100
    next_funding_time_ms: int


# ---------- fetch ----------


def fetch_binance_funding() -> list[FundingOpp]:
    try:
        req = urllib.request.Request(BINANCE_FUNDING_URL, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        logger.error("Binance funding fetch failed: %s", e)
        return []
    out = []
    for row in data:
        try:
            sym = row["symbol"]
            rate = float(row["lastFundingRate"])
            mark = float(row["markPrice"])
            nft = int(row["nextFundingTime"])
            annualized = rate * 3 * 365 * 100
            out.append(FundingOpp(sym, mark, rate, annualized, nft))
        except (KeyError, ValueError):
            continue
    return out


def rank_opps(opps: list[FundingOpp], min_rate: float) -> list[FundingOpp]:
    filt = [o for o in opps if abs(o.annualized_pct) >= min_rate]
    return sorted(filt, key=lambda o: abs(o.annualized_pct), reverse=True)


# ---------- state ----------


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"funding_history": {}, "last_scan": 0}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def update_history(state: dict, opps: list[FundingOpp]) -> None:
    hist = state.setdefault("funding_history", {})
    for o in opps:
        h = hist.setdefault(o.symbol, [])
        h.append(o.funding_rate)
        if len(h) > 20:
            del h[:-20]


def check_same_sign_history(state: dict, symbol: str, cur_rate: float) -> bool:
    hist = state.get("funding_history", {}).get(symbol, [])
    needed = MIN_FUNDING_HISTORY_HOURS // 8
    if len(hist) < needed:
        return False
    recent = hist[-needed:]
    want_pos = cur_rate > 0
    return all((r > 0) == want_pos for r in recent)


# ---------- portfolio ----------


def load_portfolio() -> dict:
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


def save_portfolio_atomic(pf: dict) -> None:
    tmp = PORTFOLIO_FILE.with_suffix(".json.tmp." + str(os.getpid()))
    tmp.write_text(json.dumps(pf, indent=2))
    tmp.replace(PORTFOLIO_FILE)


def existing_dn_positions(pf: dict) -> list[dict]:
    return [
        p
        for p in pf.get("positions", [])
        if isinstance(p, dict) and p.get("worker") == WORKER_NAME and not p.get("resolved")
    ]


def position_id(symbol: str) -> str:
    return f"dn_{symbol.lower()}"


def open_position(pf: dict, opp: FundingOpp, size_usd: float) -> dict:
    now = time.time()
    pos = {
        "id": position_id(opp.symbol),
        "worker": WORKER_NAME,
        "symbol": opp.symbol,
        "direction": "DELTA_NEUTRAL",
        "entry_price": opp.mark_price,
        "size_usd": size_usd,
        "confidence": min(0.95, abs(opp.annualized_pct) / 50),
        "entry_time": now,
        "horizon_hours": 720,  # 30-day default, rolls if sign holds
        "resolved": False,
        "exit_price": 0.0,
        "pnl_usd": 0.0,
        "correct": False,
        "resolve_time": 0.0,
        "high_water_mark": size_usd,
        "low_water_mark": size_usd,
        "trailing_stop_pct": 0.0,
        "trailing_triggered": False,
        # delta-neutral specific
        "entry_funding_rate": opp.funding_rate,
        "entry_annualized_pct": opp.annualized_pct,
        "entry_sign": 1 if opp.funding_rate > 0 else -1,
        "notional_usd": size_usd,
        "last_funding_time_ms": opp.next_funding_time_ms,
        "cumulative_funding_usd": 0.0,
        "last_update": now,
    }
    pf.setdefault("positions", []).append(pos)
    return pos


def accrue_and_check_flip(pos: dict, cur_opp: FundingOpp | None) -> str:
    """Accrue funding revenue since last update. Return 'hold', 'flip', or 'missing'."""
    now = time.time()
    last = pos.get("last_update", pos.get("entry_time", now))
    notional = pos.get("notional_usd", pos.get("size_usd", 0))
    if cur_opp is None:
        pos["last_update"] = now
        return "missing"
    # Funding cycles elapsed (3 per day, 8h each)
    dt_days = max(0.0, (now - last) / 86400.0)
    cycles_elapsed = dt_days * 3.0
    # Short perp earns funding when rate > 0; long perp earns when rate < 0.
    # We're sign-agnostic at entry — always take the correct side — so revenue
    # is |rate| * cycles_elapsed * notional per cycle.
    rev = abs(cur_opp.funding_rate) * cycles_elapsed * notional
    pos["cumulative_funding_usd"] = round(pos.get("cumulative_funding_usd", 0) + rev, 8)
    pos["size_usd"] = round(notional + pos["cumulative_funding_usd"], 8)
    pos["pnl_usd"] = round(pos["cumulative_funding_usd"], 8)
    pos["high_water_mark"] = max(pos.get("high_water_mark", notional), pos["size_usd"])
    pos["low_water_mark"] = min(pos.get("low_water_mark", notional), pos["size_usd"])
    pos["last_update"] = now
    # Sign flip?
    cur_sign = 1 if cur_opp.funding_rate > 0 else -1
    if UNWIND_ON_SIGN_FLIP and cur_sign != pos.get("entry_sign", cur_sign):
        return "flip"
    return "hold"


def resolve_position(pos: dict, reason: str) -> None:
    pos["resolved"] = True
    pos["resolve_time"] = time.time()
    pos["exit_price"] = pos.get("entry_price", 0)
    pos["correct"] = pos.get("pnl_usd", 0) > 0
    pos["resolve_reason"] = reason


# ---------- status ----------


def write_status(
    open_positions: list[dict],
    total_deployed: float,
    opps: list[FundingOpp],
    qualifying: int,
    ok: bool,
    error_msg: str | None = None,
) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(UTC).astimezone().isoformat()
    total_pnl = sum(p.get("pnl_usd", 0) for p in open_positions)
    avg_apy = 0.0
    if open_positions:
        avg_apy = sum(abs(p.get("entry_annualized_pct", 0)) for p in open_positions) / len(open_positions)
    status = {
        "worker_name": WORKER_NAME,
        "strategy_type": "delta_neutral",
        "status": "active" if ok else "degraded",
        "last_heartbeat": now_iso,
        "cycle_count": 1,
        "position_summary": {
            "open_positions": len(open_positions),
            "closed_positions": 0,
            "total_capital_deployed_usd": round(total_deployed, 4),
            "realized_pnl_usd": 0.0,
            "unrealized_pnl_usd": round(total_pnl, 6),
        },
        "performance": {
            "avg_entry_apy_pct": round(avg_apy, 4),
            "pnl_all_time": round(total_pnl, 6),
            "trades_last_24h": 0,
            "win_rate": None,
        },
        "risk": {
            "position_sizing_method": "fixed_max_position_usd",
            "max_position_usd": MAX_POSITION_USD,
            "max_total_deployed_usd": MAX_TOTAL_DEPLOYED,
            "unwind_on_sign_flip": UNWIND_ON_SIGN_FLIP,
            "paper_mode_relaxed_gate": PAPER_MODE_RELAXED_GATE,
            "counterparty": "binance_perp_public",
        },
        "strategy_config": {
            "source": "binance_public_funding",
            "min_annualized_rate_pct": MIN_ANNUALIZED_RATE,
            "min_funding_history_hours": MIN_FUNDING_HISTORY_HOURS,
            "universe_size": len(opps),
            "qualifying_count": qualifying,
            "fund_sleeves": [
                "fund_60_40_income.delta_neutral",
                "fund_75_25_balanced.delta_neutral",
            ],
        },
        "errors_last_24h": 0 if ok else 1,
        "health_check": "green" if ok else "yellow",
    }
    if error_msg:
        status["last_error"] = error_msg
    STATUS_FILE.write_text(json.dumps(status, indent=2))


# ---------- modes ----------


def run_once() -> None:
    state = load_state()
    opps = fetch_binance_funding()
    if not opps:
        write_status([], 0.0, [], 0, ok=False, error_msg="binance fetch returned no data")
        logger.warning("delta_neutral: no funding data")
        return

    update_history(state, opps)
    ranked = rank_opps(opps, MIN_ANNUALIZED_RATE)
    by_symbol = {o.symbol: o for o in opps}

    pf = load_portfolio()
    open_dn = existing_dn_positions(pf)

    # 1. Accrue + sign-flip check on existing open positions
    for pos in open_dn:
        cur = by_symbol.get(pos.get("symbol", ""))
        result = accrue_and_check_flip(pos, cur)
        if result == "flip":
            resolve_position(pos, "funding_sign_flipped")
            logger.info(
                "delta_neutral: resolved %s on sign flip, pnl=%.6f", pos.get("symbol"), pos.get("pnl_usd", 0)
            )

    # 2. Recount still-open positions after resolves
    open_dn = existing_dn_positions(pf)
    total_deployed = sum(p.get("notional_usd", p.get("size_usd", 0)) for p in open_dn)

    # 3. Open new positions until we hit MAX_OPEN_POSITIONS or MAX_TOTAL_DEPLOYED
    have_symbols = {p.get("symbol") for p in open_dn}
    for opp in ranked:
        if len(open_dn) >= MAX_OPEN_POSITIONS:
            break
        if opp.symbol in have_symbols:
            continue
        if total_deployed + MAX_POSITION_USD > MAX_TOTAL_DEPLOYED:
            break
        # Gate: same-sign history OR paper-mode relaxed
        if not PAPER_MODE_RELAXED_GATE and not check_same_sign_history(state, opp.symbol, opp.funding_rate):
            continue
        size = MAX_POSITION_USD
        pos = open_position(pf, opp, size)
        open_dn.append(pos)
        total_deployed += size
        logger.info(
            "delta_neutral: opened %s size=$%.2f entry_apy=%.2f%%", opp.symbol, size, opp.annualized_pct
        )

    # 4. Persist everything
    save_portfolio_atomic(pf)
    state["last_scan"] = int(time.time())
    save_state(state)

    # 5. Emit status
    write_status(open_dn, total_deployed, opps, len(ranked), ok=True)

    total_pnl = sum(p.get("pnl_usd", 0) for p in open_dn)
    print(
        f"[delta_neutral] open={len(open_dn)} deployed=${total_deployed:.2f} "
        f"cum_funding=${total_pnl:.6f} universe={len(opps)} qualifying={len(ranked)}"
    )


def scan(min_rate: float) -> None:
    state = load_state()
    opps = fetch_binance_funding()
    if not opps:
        logger.warning("No funding data available")
        return
    update_history(state, opps)
    ranked = rank_opps(opps, min_rate)
    qualified = [(o, check_same_sign_history(state, o.symbol, o.funding_rate)) for o in ranked[:20]]

    print("\n=== Delta-Neutral Funding Scan ===")
    print(f"Universe: {len(opps)} Binance perps | min rate: {min_rate}% annualized")
    print(f"Qualifying: {len(ranked)} | same-sign history required: {MIN_FUNDING_HISTORY_HOURS}h")
    print()
    print(f"{'SYM':<14} {'ANNLZ%':>8} {'PER-CYC':>10} {'MARK':>14} {'GATE':>6} {'ACTION':<26}")
    for o, ss in qualified[:15]:
        action = "LONG SPOT + SHORT PERP" if o.annualized_pct > 0 else "SHORT SPOT + LONG PERP"
        gate = "OK" if ss else "wait"
        print(
            f"{o.symbol:<14} {o.annualized_pct:>+8.2f} {o.funding_rate:>+10.6f} {o.mark_price:>14.4f} {gate:>6} {action:<26}"
        )

    state["last_scan"] = int(time.time())
    save_state(state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="Scan mode (read-only)")
    ap.add_argument(
        "--min-rate",
        type=float,
        default=MIN_ANNUALIZED_RATE,
        help="Minimum annualized funding rate (percent)",
    )
    args = ap.parse_args()
    if args.scan:
        scan(args.min_rate)
    else:
        run_once()


if __name__ == "__main__":
    main()
