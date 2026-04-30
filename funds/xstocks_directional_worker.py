#!/usr/bin/env python3
"""
xstocks_directional_worker -- paper scanner for directional tokenized-stock trades.

Contract (matches scanner-worker invariants in funds/README.md):
  - Tags paper positions with worker='xstocks_directional'
  - Upserts into ~/.hermes/brain/paper_portfolio.json
  - Emits status at  ~/.hermes/brain/status/xstocks_directional.json
  - Persists state at ~/.hermes/brain/state/xstocks_directional_state.json

Strategy (paper, MVP):
  - Basket: 4 megacap tokenized-stock proxies (non-overlapping with xstocks_grid)
      AAPLx <- AAPL, AMZNx <- AMZN, MSFTx <- MSFT, GOOGLx <- GOOGL
  - Data: Yahoo Finance chart endpoint (free, no key, 30d daily closes)
  - Signal (per symbol, per cycle):
      entry  = close > 20d SMA AND 5d return >= +1.0%
      exit   = close < 20d SMA OR 5d return <= -1.0%
      stop   = mark-to-market <= -7%
  - Position sizing: $35 per entry, max 4 open
  - Paper only. No real order submission.

Fund coverage: fund_90_10_growth.xstocks_directional  (the last unfunded sleeve)
"""

import json
import os
import statistics
import urllib.request
from datetime import datetime
from pathlib import Path

from engine.logging_setup import setup_logger
from engine.policy import sleeve_targets_for, worker_cfg

WORKER_NAME = "xstocks_directional"
PORTFOLIO_FILE = Path.home() / ".hermes/brain/paper_portfolio.json"
STATUS_FILE = Path.home() / ".hermes/brain/status/xstocks_directional.json"
STATE_FILE = Path.home() / ".hermes/brain/state/xstocks_directional_state.json"

UNIVERSE = [
    {"symbol": "AAPLx", "yahoo": "AAPL", "underlying": "AAPL"},
    {"symbol": "AMZNx", "yahoo": "AMZN", "underlying": "AMZN"},
    {"symbol": "MSFTx", "yahoo": "MSFT", "underlying": "MSFT"},
    {"symbol": "GOOGLx", "yahoo": "GOOGL", "underlying": "GOOGL"},
]

_cfg = worker_cfg("xstocks_directional")
_targets = sleeve_targets_for("xstocks_directional")
FUND_SLEEVES = list(_targets.keys()) or ["fund_90_10_growth.xstocks_directional"]
PRINCIPAL_USD = next(iter(_targets.values()), 25.00) if _targets else 25.00
MAX_OPEN_POSITIONS = _cfg.get("max_open_positions", 4)
SMA_WINDOW = _cfg.get("sma_window", 20)
MOMENTUM_WINDOW = _cfg.get("momentum_window", 5)
ENTRY_MOMENTUM_PCT = _cfg.get("entry_momentum_pct", 1.0)
EXIT_MOMENTUM_PCT = _cfg.get("exit_momentum_pct", -1.0)
STOP_LOSS_PCT = _cfg.get("stop_loss_pct", -7.0)

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=45d"

log = setup_logger(WORKER_NAME)


def fetch_closes(yahoo_sym):
    """Return (closes_list, last_close) or (None, None) on failure."""
    url = YAHOO_URL.format(sym=yahoo_sym)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        result = d["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        closes = [c for c in closes if c is not None]
        if len(closes) < SMA_WINDOW + 1:
            return None, None
        return closes, closes[-1]
    except Exception as e:
        log.warning("fetch_closes %s err: %s", yahoo_sym, e)
        return None, None


def compute_signal(closes):
    """Return dict with sma20, mom_5d_pct, last_close, signal."""
    last = closes[-1]
    sma20 = statistics.mean(closes[-SMA_WINDOW:])
    ref_5d = closes[-MOMENTUM_WINDOW - 1]
    mom_pct = ((last - ref_5d) / ref_5d) * 100.0
    above_sma = last > sma20
    if above_sma and mom_pct >= ENTRY_MOMENTUM_PCT:
        sig = "ENTER_LONG"
    elif (not above_sma) or mom_pct <= EXIT_MOMENTUM_PCT:
        sig = "EXIT"
    else:
        sig = "HOLD"
    return {
        "last_close": round(last, 4),
        "sma20": round(sma20, 4),
        "mom_5d_pct": round(mom_pct, 3),
        "above_sma20": above_sma,
        "signal": sig,
    }


def load_json(p, default):
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def save_json_atomic(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(p)


def run_once():
    now_iso = datetime.now().astimezone().isoformat()

    portfolio = load_json(PORTFOLIO_FILE, {"positions": []})
    positions = portfolio.get("positions", []) if isinstance(portfolio, dict) else portfolio

    # Existing open positions owned by this worker
    my_open = {
        p["symbol"]: p
        for p in positions
        if isinstance(p, dict) and p.get("worker") == WORKER_NAME and not p.get("resolved")
    }

    opened = []
    exited = []
    stopped = []
    held = []
    fetch_errors = 0
    signal_log = {}

    for u in UNIVERSE:
        closes, _ = fetch_closes(u["yahoo"])
        if not closes:
            fetch_errors += 1
            continue
        sig = compute_signal(closes)
        signal_log[u["symbol"]] = sig
        mark = sig["last_close"]

        existing = my_open.get(u["symbol"])
        if existing:
            entry = existing.get("entry_price") or mark
            pnl_pct = ((mark - entry) / entry) * 100.0
            existing["mark_price"] = mark
            existing["pnl_pct"] = round(pnl_pct, 3)
            existing["pnl_usd"] = round(
                (mark - entry) / entry * existing.get("size_usd", PRINCIPAL_USD), 4
            )
            existing["last_mark_iso"] = now_iso
            existing["last_signal"] = sig["signal"]

            if pnl_pct <= STOP_LOSS_PCT:
                existing["resolved"] = True
                existing["resolved_reason"] = "stop_loss"
                existing["resolved_iso"] = now_iso
                stopped.append(u["symbol"])
            elif sig["signal"] == "EXIT":
                existing["resolved"] = True
                existing["resolved_reason"] = "signal_exit"
                existing["resolved_iso"] = now_iso
                exited.append(u["symbol"])
            else:
                held.append(u["symbol"])
        else:
            open_count = len([s for s in my_open if not my_open[s].get("resolved")])
            open_count -= len(exited) + len(stopped)
            if sig["signal"] == "ENTER_LONG" and open_count < MAX_OPEN_POSITIONS:
                new_pos = {
                    "id": f"{WORKER_NAME}_{u['symbol']}_{int(datetime.now().timestamp())}",
                    "worker": WORKER_NAME,
                    "fund": FUND_SLEEVES[0].split(".", 1)[0],
                    "sleeve": FUND_SLEEVES[0],
                    "symbol": u["symbol"],
                    "underlying": u["underlying"],
                    "side": "long",
                    "entry_price": mark,
                    "mark_price": mark,
                    "size_usd": PRINCIPAL_USD,
                    "pnl_pct": 0.0,
                    "pnl_usd": 0.0,
                    "opened_iso": now_iso,
                    "last_mark_iso": now_iso,
                    "resolved": False,
                    "signal_at_entry": sig,
                }
                positions.append(new_pos)
                my_open[u["symbol"]] = new_pos
                opened.append(u["symbol"])

    # persist portfolio
    if isinstance(portfolio, dict):
        portfolio["positions"] = positions
        save_json_atomic(PORTFOLIO_FILE, portfolio)
    else:
        save_json_atomic(PORTFOLIO_FILE, positions)

    open_mine = [
        p
        for p in positions
        if isinstance(p, dict) and p.get("worker") == WORKER_NAME and not p.get("resolved")
    ]
    deployed = sum(p.get("size_usd") or 0 for p in open_mine)
    unrealized = sum(p.get("pnl_usd") or 0 for p in open_mine)

    state = load_json(STATE_FILE, {})
    cycle = int(state.get("cycle_count", 0)) + 1

    status = {
        "worker_name": WORKER_NAME,
        "strategy_type": "directional_sma_momentum",
        "status": "active" if open_mine else "scanning",
        "last_heartbeat": now_iso,
        "cycle_count": cycle,
        "position_summary": {
            "open_positions": len(open_mine),
            "deployed_usd": round(deployed, 2),
            "unrealized_pnl_usd": round(unrealized, 4),
            "max_positions": MAX_OPEN_POSITIONS,
        },
        "this_cycle": {
            "opened": opened,
            "exited": exited,
            "stopped": stopped,
            "held": held,
            "fetch_errors": fetch_errors,
            "signals": signal_log,
        },
        "config": {
            "principal_usd": PRINCIPAL_USD,
            "sma_window": SMA_WINDOW,
            "momentum_window": MOMENTUM_WINDOW,
            "entry_threshold_pct": ENTRY_MOMENTUM_PCT,
            "exit_threshold_pct": EXIT_MOMENTUM_PCT,
            "stop_loss_pct": STOP_LOSS_PCT,
            "universe": [u["symbol"] for u in UNIVERSE],
            "data_source": "yahoo_finance_chart_v8",
        },
        "fund_sleeves": FUND_SLEEVES,
    }
    save_json_atomic(STATUS_FILE, status)

    state["cycle_count"] = cycle
    state["last_run"] = now_iso
    save_json_atomic(STATE_FILE, state)

    log.info(
        "cycle=%d opened=%s exited=%s stopped=%s held=%s deployed=$%.2f",
        cycle,
        opened,
        exited,
        stopped,
        held,
        deployed,
    )
    return status


if __name__ == "__main__":
    run_once()
