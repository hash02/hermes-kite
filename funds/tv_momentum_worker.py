#!/usr/bin/env python3
"""
tv_momentum_worker — paper scanner for crypto directional momentum.

Contract (matches aave_usdc + delta_neutral_funding + polymarket_btc_updown + xstocks_grid):
  - Tags paper positions with worker='tv_momentum'
  - Upserts into ~/.hermes/brain/paper_portfolio.json
  - Emits status at ~/.hermes/brain/status/tv_momentum.json

Strategy (paper, MVP):
  - Universe: BTCUSDT, ETHUSDT, SOLUSDT (Binance spot, public API, no auth)
  - Signal: 7-day return from daily klines
  - Entry: 7d return >= +5% AND no open position for that symbol
  - Exit/resolve: 7d return <= +2% (momentum faded) OR stop at -8% from entry
  - Mark-to-market: every cycle reprices size_usd = shares * current_close
  - Principal: $40 per position, up to 3 open
  - No hedge, no leverage; pure directional paper.

Fund coverage: fund_90_10_growth.directional_momentum
"""

import json
import logging
import os
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

WORKER_NAME = "tv_momentum"
PORTFOLIO_FILE = Path.home() / ".hermes/brain/paper_portfolio.json"
STATUS_FILE = Path.home() / ".hermes/brain/status/tv_momentum.json"
STATE_FILE = Path.home() / ".hermes/brain/state/tv_momentum_state.json"

UNIVERSE = [
    {"symbol": "BTC", "binance": "BTCUSDT"},
    {"symbol": "ETH", "binance": "ETHUSDT"},
    {"symbol": "SOL", "binance": "SOLUSDT"},
]
PRINCIPAL_USD = 40.00
MAX_OPEN_POSITIONS = 3
ENTRY_MOMENTUM_PCT = 2.0  # enter if 7d >= +2% (paper: any positive trend)
EXIT_MOMENTUM_PCT = -2.0  # exit only on reversal below -2%
STOP_LOSS_PCT = -8.0  # stop if unrealized <= -8% from entry
LOOKBACK_DAYS = 7
FUND_SLEEVES = ["fund_90_10_growth.directional_momentum"]

KLINES_URL = "https://api.binance.com/api/v3/klines?symbol={sym}&interval=1d&limit=10"

log = logging.getLogger(WORKER_NAME)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def fetch_klines(binance_sym):
    url = KLINES_URL.format(sym=binance_sym)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 hermes-kite"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    # klines: [open_time, open, high, low, close, volume, close_time, ...]
    closes = [float(k[4]) for k in data]
    return closes


def compute_momentum(closes):
    """Return (current_close, pct_return_over_lookback_days) or (None, None)."""
    if not closes or len(closes) < LOOKBACK_DAYS + 1:
        return None, None
    cur = closes[-1]
    prev = closes[-1 - LOOKBACK_DAYS]
    if prev <= 0:
        return cur, None
    pct = ((cur - prev) / prev) * 100.0
    return cur, pct


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
    resolved = 0
    stopped = 0
    marked = 0
    fetch_errors = 0

    # count existing open
    open_self = [
        p
        for p in positions
        if isinstance(p, dict) and p.get("worker") == WORKER_NAME and not p.get("resolved")
    ]

    for item in UNIVERSE:
        try:
            closes = fetch_klines(item["binance"])
        except Exception as e:
            fetch_errors += 1
            log.warning("klines fetch failed for %s: %s", item["binance"], e)
            continue

        cur_price, mom_pct = compute_momentum(closes)
        if cur_price is None:
            fetch_errors += 1
            continue

        existing = find_pos(positions, item["symbol"])

        if existing is None:
            # consider entry
            if mom_pct is None or mom_pct < ENTRY_MOMENTUM_PCT:
                continue
            if len(open_self) + opened >= MAX_OPEN_POSITIONS:
                continue
            shares = PRINCIPAL_USD / cur_price
            pos = {
                "id": f"tv_momentum_{item['symbol']}",
                "worker": WORKER_NAME,
                "symbol": item["symbol"],
                "binance_symbol": item["binance"],
                "direction": "long",
                "principal_usd": PRINCIPAL_USD,
                "entry_price": cur_price,
                "shares": shares,
                "size_usd": PRINCIPAL_USD,
                "pnl_usd": 0.0,
                "last_price": cur_price,
                "last_mark_time": now_iso,
                "entry_time": now_iso,
                "entry_momentum_pct": round(mom_pct, 3),
                "resolved": False,
            }
            positions.append(pos)
            opened += 1
            actions.append(f"open {item['symbol']}@${cur_price:.2f} (7d={mom_pct:+.2f}%)")
            log.info("opened %s @ $%.2f 7d=%+.2f%%", item["symbol"], cur_price, mom_pct)
            continue

        # mark-to-market existing
        shares = existing.get("shares") or (existing["principal_usd"] / existing["entry_price"])
        existing["shares"] = shares
        existing["last_price"] = cur_price
        existing["last_mark_time"] = now_iso
        existing["size_usd"] = round(shares * cur_price, 4)
        existing["pnl_usd"] = round(existing["size_usd"] - existing["principal_usd"], 4)
        entry = existing.get("entry_price") or cur_price
        pnl_pct = ((cur_price - entry) / entry) * 100.0 if entry else 0.0
        existing["pnl_pct"] = round(pnl_pct, 3)
        existing["current_momentum_pct"] = round(mom_pct, 3) if mom_pct is not None else None
        marked += 1

        # exits
        if mom_pct is not None and mom_pct <= EXIT_MOMENTUM_PCT:
            existing["resolved"] = True
            existing["resolved_at"] = now_iso
            existing["resolve_reason"] = f"momentum_fade_7d={mom_pct:.2f}%"
            resolved += 1
            actions.append(f"resolve {item['symbol']} (7d={mom_pct:+.2f}%)")
            log.info("resolved %s @ $%.2f mom=%+.2f%%", item["symbol"], cur_price, mom_pct)
        elif pnl_pct <= STOP_LOSS_PCT:
            existing["resolved"] = True
            existing["resolved_at"] = now_iso
            existing["resolve_reason"] = f"stop_loss_{pnl_pct:.2f}%"
            stopped += 1
            actions.append(f"stop {item['symbol']} pnl={pnl_pct:+.2f}%")
            log.info("stopped %s @ $%.2f pnl=%+.2f%%", item["symbol"], cur_price, pnl_pct)

    # persist portfolio
    if isinstance(portfolio, dict):
        portfolio["positions"] = positions
        save_json_atomic(PORTFOLIO_FILE, portfolio)
    else:
        save_json_atomic(PORTFOLIO_FILE, positions)

    # status
    open_tv = [
        p
        for p in positions
        if isinstance(p, dict) and p.get("worker") == WORKER_NAME and not p.get("resolved")
    ]
    deployed = sum(p.get("size_usd") or 0 for p in open_tv)
    unrealized = sum(p.get("pnl_usd") or 0 for p in open_tv)

    state = load_json(STATE_FILE, {})
    cycle = int(state.get("cycle_count", 0)) + 1

    status = {
        "worker_name": WORKER_NAME,
        "strategy_type": "tv_momentum",
        "status": "active" if open_tv else "scanning",
        "last_heartbeat": datetime.now().astimezone().isoformat(),
        "cycle_count": cycle,
        "position_summary": {
            "open_positions": len(open_tv),
            "deployed_usd": round(deployed, 2),
            "unrealized_pnl_usd": round(unrealized, 2),
            "max_positions": MAX_OPEN_POSITIONS,
        },
        "this_cycle": {
            "opened": opened,
            "resolved": resolved,
            "stopped": stopped,
            "marked": marked,
            "fetch_errors": fetch_errors,
            "actions": actions,
        },
        "config": {
            "principal_usd": PRINCIPAL_USD,
            "lookback_days": LOOKBACK_DAYS,
            "entry_threshold_pct": ENTRY_MOMENTUM_PCT,
            "exit_threshold_pct": EXIT_MOMENTUM_PCT,
            "stop_loss_pct": STOP_LOSS_PCT,
            "universe": [u["symbol"] for u in UNIVERSE],
        },
        "fund_sleeves": FUND_SLEEVES,
    }
    save_json_atomic(STATUS_FILE, status)

    state["cycle_count"] = cycle
    state["last_run"] = now_iso
    save_json_atomic(STATE_FILE, state)

    log.info(
        "cycle=%d opened=%d resolved=%d stopped=%d marked=%d deployed=$%.2f",
        cycle,
        opened,
        resolved,
        stopped,
        marked,
        deployed,
    )
    return status


if __name__ == "__main__":
    run_once()
