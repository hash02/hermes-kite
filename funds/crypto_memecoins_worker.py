#!/usr/bin/env python3
"""
crypto_memecoins_worker — paper momentum sniper for large-cap memecoins.

Signal: 7-day % change from CoinGecko's public /coins/markets endpoint
filtered to the 'meme-token' category. Enter on +20% 7d, exit on -10% 7d
or -20% stop. Keeps sizing tiny since the fat-tail downside dominates.

Pairs with wow_sniper_base in fund_90_10_growth.memecoin_sniper. Each worker
takes half the sleeve target ($50 of $100). Positions tagged with `fund`
and `sleeve` for per-fund routing.

Data source: https://api.coingecko.com/api/v3/coins/markets (free tier, no key)
"""
from __future__ import annotations
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

WORKER_NAME = "crypto_memecoins"
HERMES = Path.home() / ".hermes" / "brain"
PORTFOLIO_FILE = HERMES / "paper_portfolio.json"
STATUS_FILE = HERMES / "status" / f"{WORKER_NAME}.json"
STATE_FILE = HERMES / "state" / f"{WORKER_NAME}_state.json"

SLEEVE_TARGETS = {
    "fund_90_10_growth.memecoin_sniper": 50.00,  # half of $100 sleeve (wow_sniper_base takes the other half)
}

MAX_OPEN_POSITIONS = 5           # $50 / 5 = $10 per position
ENTRY_7D_PCT = 20.0              # must be +20% over 7 days to enter
EXIT_7D_PCT = -10.0              # exit on momentum fade
STOP_LOSS_PCT = -20.0            # hard stop from entry
MIN_MARKET_CAP_USD = 100_000_000  # top-tier memecoins only

CG_URL = (
    "https://api.coingecko.com/api/v3/coins/markets"
    "?vs_currency=usd&category=meme-token&order=market_cap_desc"
    "&per_page=50&page=1&price_change_percentage=7d"
)

log = logging.getLogger(WORKER_NAME)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

UA = {"User-Agent": "hermes-crypto-memecoins/1.0"}


def fetch_memecoins() -> list[dict]:
    req = urllib.request.Request(CG_URL, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception as e:
        log.warning("coingecko fetch failed: %s", e)
        return []
    out = []
    for row in data:
        try:
            mcap = float(row.get("market_cap") or 0)
            if mcap < MIN_MARKET_CAP_USD:
                continue
            out.append({
                "id": row["id"],
                "symbol": row["symbol"].upper(),
                "price": float(row["current_price"]),
                "mcap": mcap,
                "change_7d": float(row.get("price_change_percentage_7d_in_currency") or 0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def load_json(p, default):
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def save_json_atomic(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(p)


def positions_for_sleeve(positions, sleeve_id, open_only=True):
    out = []
    for p in positions:
        if not isinstance(p, dict):
            continue
        if p.get("worker") != WORKER_NAME:
            continue
        if p.get("sleeve") != sleeve_id:
            continue
        if open_only and p.get("resolved"):
            continue
        out.append(p)
    return out


def run_once():
    portfolio = load_json(PORTFOLIO_FILE, {"positions": []})
    positions = portfolio.get("positions", []) if isinstance(portfolio, dict) else portfolio
    now_iso = datetime.now(timezone.utc).isoformat()
    universe = fetch_memecoins()
    by_id = {m["id"]: m for m in universe}

    opened = 0
    resolved = 0

    for sleeve_id, target_usd in SLEEVE_TARGETS.items():
        fund_id = sleeve_id.split(".", 1)[0]
        slot_size = round(target_usd / MAX_OPEN_POSITIONS, 4)
        open_pos = positions_for_sleeve(positions, sleeve_id)

        # mark-to-market + exit
        for pos in open_pos:
            cur = by_id.get(pos.get("coingecko_id"))
            if cur is None:
                continue
            entry = pos.get("entry_price") or cur["price"]
            shares = pos.get("principal_usd", slot_size) / entry if entry else 0
            mark = cur["price"]
            pnl_pct = ((mark - entry) / entry) * 100.0 if entry else 0.0
            pos["mark_price"] = mark
            pos["size_usd"] = round(shares * mark, 4)
            pos["pnl_usd"] = round(pos["size_usd"] - pos["principal_usd"], 4)
            pos["last_mark_time"] = now_iso
            pos["current_change_7d_pct"] = cur["change_7d"]

            if cur["change_7d"] <= EXIT_7D_PCT:
                pos["resolved"] = True
                pos["resolve_time"] = now_iso
                pos["resolve_reason"] = f"momentum_fade_7d={cur['change_7d']:.2f}%"
                pos["correct"] = pos["pnl_usd"] > 0
                resolved += 1
            elif pnl_pct <= STOP_LOSS_PCT:
                pos["resolved"] = True
                pos["resolve_time"] = now_iso
                pos["resolve_reason"] = f"stop_loss_{pnl_pct:.2f}%"
                pos["correct"] = False
                resolved += 1

        open_pos = positions_for_sleeve(positions, sleeve_id)
        open_ids = {p.get("coingecko_id") for p in open_pos}
        for cand in sorted(universe, key=lambda x: -x["change_7d"]):
            if len(open_pos) >= MAX_OPEN_POSITIONS:
                break
            if cand["id"] in open_ids:
                continue
            if cand["change_7d"] < ENTRY_7D_PCT:
                break  # sorted desc — rest will also fail
            pos = {
                "id": f"{WORKER_NAME}:{sleeve_id}:{cand['id']}:{int(time.time())}",
                "worker": WORKER_NAME,
                "fund": fund_id,
                "sleeve": sleeve_id,
                "symbol": cand["symbol"],
                "coingecko_id": cand["id"],
                "direction": "long",
                "entry_price": cand["price"],
                "mark_price": cand["price"],
                "principal_usd": slot_size,
                "size_usd": slot_size,
                "pnl_usd": 0.0,
                "entry_time": now_iso,
                "last_mark_time": now_iso,
                "entry_change_7d_pct": cand["change_7d"],
                "mcap_at_entry_usd": cand["mcap"],
                "resolved": False,
            }
            positions.append(pos)
            open_pos.append(pos)
            open_ids.add(cand["id"])
            opened += 1

    if isinstance(portfolio, dict):
        portfolio["positions"] = positions
        save_json_atomic(PORTFOLIO_FILE, portfolio)
    else:
        save_json_atomic(PORTFOLIO_FILE, positions)

    all_open = [p for p in positions if isinstance(p, dict)
                and p.get("worker") == WORKER_NAME and not p.get("resolved")]
    deployed = sum(p.get("size_usd", 0) for p in all_open)
    unrealized = sum(p.get("pnl_usd", 0) for p in all_open)

    save_json_atomic(STATUS_FILE, {
        "worker_name": WORKER_NAME,
        "strategy_type": "memecoin_momentum",
        "status": "active" if all_open else "scanning",
        "last_heartbeat": datetime.now().astimezone().isoformat(),
        "position_summary": {
            "open_positions": len(all_open),
            "total_capital_deployed_usd": round(deployed, 4),
            "unrealized_pnl_usd": round(unrealized, 4),
        },
        "this_cycle": {"opened": opened, "resolved": resolved,
                       "universe_size": len(universe)},
        "risk": {
            "position_sizing_method": "even_slot",
            "sleeve_targets_usd": SLEEVE_TARGETS,
            "max_open_positions": MAX_OPEN_POSITIONS,
            "entry_7d_pct": ENTRY_7D_PCT,
            "exit_7d_pct": EXIT_7D_PCT,
            "stop_loss_pct": STOP_LOSS_PCT,
            "min_market_cap_usd": MIN_MARKET_CAP_USD,
        },
        "strategy_config": {
            "source": "coingecko_public_meme_category",
            "fund_sleeves": list(SLEEVE_TARGETS.keys()),
        },
        "errors_last_24h": 0,
        "health_check": "green",
    })

    print(f"[{WORKER_NAME}] open={len(all_open)} deployed=${deployed:.2f} "
          f"unrealized=${unrealized:.4f} opened={opened} resolved={resolved} "
          f"universe={len(universe)}")


if __name__ == "__main__":
    run_once()
