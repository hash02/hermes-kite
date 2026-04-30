#!/usr/bin/env python3
"""
xstocks_grid_worker — paper scanner for tokenized-stock basket.

Contract (matches aave_usdc + delta_neutral_funding + polymarket_btc_updown):
  - Tags paper positions with worker='xstocks_grid'
  - Upserts into ~/.hermes/brain/paper_portfolio.json
  - Emits status at ~/.hermes/brain/status/xstocks_grid.json

Strategy (paper, MVP):
  - Basket of 4 symbols (proxies for Solana xStocks tokens):
      TSLAx <- TSLA, NVDAx <- NVDA, COINx <- COIN, MSTRx <- MSTR
  - Data: Stooq free CSV (no auth, no key)
  - Each symbol: open 1 paper position, principal $30, track mark-to-close PnL
  - Grid logic: if price drops >=5% from entry, double-down (add $15 at lower price)
                if price rises >=10% from entry, trim half (realize partial pnl)
  - No hard stop; paper basket is buy-and-mark

Fund coverage: fund_75_25_balanced.tokenized_stocks
"""

import json
import logging
import os
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

WORKER_NAME = "xstocks_grid"
PORTFOLIO_FILE = Path.home() / ".hermes/brain/paper_portfolio.json"
STATUS_FILE = Path.home() / ".hermes/brain/status/xstocks_grid.json"
STATE_FILE = Path.home() / ".hermes/brain/state/xstocks_grid_state.json"

BASKET = [
    {"symbol": "TSLAx", "stooq": "tsla.us", "underlying": "TSLA"},
    {"symbol": "NVDAx", "stooq": "nvda.us", "underlying": "NVDA"},
    {"symbol": "COINx", "stooq": "coin.us", "underlying": "COIN"},
    {"symbol": "MSTRx", "stooq": "mstr.us", "underlying": "MSTR"},
]
PRINCIPAL_USD = 30.00
DOUBLE_DOWN_DROP_PCT = 5.0  # add if down >=5% from avg entry
TRIM_RISE_PCT = 10.0  # realize half if up >=10% from avg entry
FUND_SLEEVES = ["fund_75_25_balanced.tokenized_stocks"]

log = logging.getLogger(WORKER_NAME)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def fetch_close(stooq_sym):
    url = f"https://stooq.com/q/l/?s={stooq_sym}&f=sd2t2ohlcv&h&e=csv"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        lines = r.read().decode().strip().splitlines()
    if len(lines) < 2:
        return None
    hdr = lines[0].split(",")
    vals = lines[1].split(",")
    row = dict(zip(hdr, vals, strict=False))
    try:
        return {
            "close": float(row.get("Close") or 0),
            "date": row.get("Date"),
            "volume": int(float(row.get("Volume") or 0)),
        }
    except Exception:
        return None


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


def find_pos(positions, symbol):
    for p in positions:
        if (
            isinstance(p, dict)
            and p.get("worker") == WORKER_NAME
            and p.get("symbol") == symbol
            and not p.get("resolved")
        ):
            return p
    return None


def run_once():
    portfolio = load_json(PORTFOLIO_FILE, {"positions": []})
    positions = portfolio.get("positions", []) if isinstance(portfolio, dict) else portfolio
    now_iso = datetime.now(UTC).isoformat()

    actions = []
    opened = 0
    double_downs = 0
    trims = 0
    price_errors = 0

    for item in BASKET:
        try:
            q = fetch_close(item["stooq"])
        except Exception as e:
            price_errors += 1
            log.warning("fetch failed for %s: %s", item["symbol"], e)
            continue
        if not q or q["close"] <= 0:
            price_errors += 1
            continue
        cur_price = q["close"]
        existing = find_pos(positions, item["symbol"])
        if existing is None:
            # open
            pos = {
                "id": f"xstocks_grid_{item['symbol']}",
                "worker": WORKER_NAME,
                "symbol": item["symbol"],
                "underlying": item["underlying"],
                "direction": "long",
                "principal_usd": PRINCIPAL_USD,
                "entry_price": cur_price,
                "avg_entry_price": cur_price,
                "shares": PRINCIPAL_USD / cur_price,
                "size_usd": PRINCIPAL_USD,
                "pnl_usd": 0.0,
                "last_price": cur_price,
                "last_mark_time": now_iso,
                "entry_time": now_iso,
                "resolved": False,
                "grid_ops": [],
            }
            positions.append(pos)
            opened += 1
            actions.append(f"open {item['symbol']}@${cur_price:.2f}")
            log.info("opened %s at $%.2f ($%.2f principal)", item["symbol"], cur_price, PRINCIPAL_USD)
            continue

        # mark-to-market
        shares = existing.get("shares") or (existing["principal_usd"] / existing["avg_entry_price"])
        existing["shares"] = shares
        existing["last_price"] = cur_price
        existing["last_mark_time"] = now_iso

        avg_entry = existing.get("avg_entry_price") or existing.get("entry_price")
        pct_move = ((cur_price - avg_entry) / avg_entry) * 100 if avg_entry else 0
        existing["size_usd"] = round(shares * cur_price, 4)
        existing["pnl_usd"] = round(existing["size_usd"] - existing["principal_usd"], 4)

        # grid ops
        if pct_move <= -DOUBLE_DOWN_DROP_PCT:
            add_usd = PRINCIPAL_USD * 0.5
            add_shares = add_usd / cur_price
            total_cost = shares * avg_entry + add_shares * cur_price
            total_shares = shares + add_shares
            existing["shares"] = total_shares
            existing["avg_entry_price"] = total_cost / total_shares
            existing["principal_usd"] = round(existing["principal_usd"] + add_usd, 4)
            existing["size_usd"] = round(total_shares * cur_price, 4)
            existing["pnl_usd"] = round(existing["size_usd"] - existing["principal_usd"], 4)
            existing.setdefault("grid_ops", []).append(
                {"op": "double_down", "price": cur_price, "add_usd": add_usd, "time": now_iso}
            )
            double_downs += 1
            actions.append(f"double_down {item['symbol']} +${add_usd:.2f}@${cur_price:.2f}")
            log.info(
                "double_down %s @ $%.2f (avg_entry now $%.2f)",
                item["symbol"],
                cur_price,
                existing["avg_entry_price"],
            )
        elif pct_move >= TRIM_RISE_PCT:
            trim_shares = shares * 0.5
            realized = trim_shares * (cur_price - avg_entry)
            existing["shares"] = shares - trim_shares
            # do not reduce principal; principal tracks cost basis paid so far
            existing["size_usd"] = round(existing["shares"] * cur_price, 4)
            existing["pnl_usd"] = round(existing["size_usd"] - existing["principal_usd"] + realized, 4)
            existing.setdefault("grid_ops", []).append(
                {"op": "trim_half", "price": cur_price, "realized": round(realized, 4), "time": now_iso}
            )
            trims += 1
            actions.append(f"trim {item['symbol']} @${cur_price:.2f} realized=${realized:.2f}")
            log.info("trim %s @ $%.2f (realized $%.2f)", item["symbol"], cur_price, realized)

    # persist portfolio
    if isinstance(portfolio, dict):
        portfolio["positions"] = positions
        save_json_atomic(PORTFOLIO_FILE, portfolio)
    else:
        save_json_atomic(PORTFOLIO_FILE, positions)

    # status
    open_xs = [
        p
        for p in positions
        if isinstance(p, dict) and p.get("worker") == WORKER_NAME and not p.get("resolved")
    ]
    deployed = sum(p.get("size_usd") or 0 for p in open_xs)
    unrealized = sum(p.get("pnl_usd") or 0 for p in open_xs)

    state = load_json(STATE_FILE, {})
    cycle = int(state.get("cycle_count", 0)) + 1

    status = {
        "worker_name": WORKER_NAME,
        "strategy_type": "xstocks_grid",
        "status": "active" if open_xs else "scanning",
        "last_heartbeat": datetime.now().astimezone().isoformat(),
        "cycle_count": cycle,
        "position_summary": {
            "open_positions": len(open_xs),
            "closed_positions": 0,
            "total_capital_deployed_usd": round(deployed, 2),
            "realized_pnl_usd": 0.0,
            "unrealized_pnl_usd": round(unrealized, 2),
        },
        "performance": {
            "pnl_all_time": round(unrealized, 2),
            "trades_last_24h": opened + double_downs + trims,
            "win_rate": None,
        },
        "risk": {
            "basket_size": len(BASKET),
            "principal_per_symbol_usd": PRINCIPAL_USD,
            "double_down_drop_pct": DOUBLE_DOWN_DROP_PCT,
            "trim_rise_pct": TRIM_RISE_PCT,
        },
        "strategy_config": {
            "source": "stooq_public_csv",
            "basket": [b["symbol"] for b in BASKET],
            "fund_sleeves": FUND_SLEEVES,
        },
        "errors_last_24h": price_errors,
        "health_check": "green" if price_errors == 0 else "yellow",
        "this_cycle": {
            "opened": opened,
            "double_downs": double_downs,
            "trims": trims,
            "price_errors": price_errors,
            "actions": actions,
        },
    }
    save_json_atomic(STATUS_FILE, status)
    state["cycle_count"] = cycle
    state["last_run"] = now_iso
    save_json_atomic(STATE_FILE, state)

    print(
        f"[{WORKER_NAME}] open={len(open_xs)} deployed=${deployed:.2f} "
        f"unrealized=${unrealized:.4f} ops={opened}/{double_downs}/{trims} errors={price_errors}"
    )


if __name__ == "__main__":
    run_once()
