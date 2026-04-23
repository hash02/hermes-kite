#!/usr/bin/env python3
"""
grid_eth_usdc scanner worker -- spot grid trader on ETH/USDC, covering the
structural_grid and aggressive_grid sleeves across all three funds.

Mechanism: Place a paper-grid of N evenly-spaced buy and sell levels around
a rolling pivot (median of the last 24 hourly closes). Every cycle, snap
the current ETH/USDC mark to the nearest grid line — if it crossed a level
since last cycle, fire one paper buy or sell at that level. Each fill is
its own resolved paper position carrying realized PnL = sell_price - buy_price.

This is the deterministic "structural" income engine: zero directional bias,
profits only when price oscillates inside the grid range. Drawdown happens
when price trends out of the band (positions sit unfilled on the wrong side).

Per-sleeve fund coverage (each sleeve gets its own grid pool, sized to target):
  - fund_60_40_income.structural_grid     ($250)
  - fund_75_25_balanced.structural_grid   ($250)
  - fund_90_10_growth.aggressive_grid     ($200)

Cycle work:
  1. Fetch current ETHUSDC mark from Binance public spot (no auth)
  2. Fetch last 24 hourly closes; pivot = median(closes); set bands
  3. Per sleeve:
     a. Initialize grid state if first run (compute levels, equal-weight slots)
     b. Compare current mark vs last_mark — for each grid level crossed,
        record a fill (buy on cross-down, sell on cross-up)
     c. If a buy fill closes a previously-open buy at a lower level, realize
        the PnL on the round-trip and resolve both legs
  4. Persist portfolio + emit status

Same scanner contract as other workers:
  - Tags positions with worker="grid_eth_usdc", fund=<fund_id>, sleeve=<sleeve_id>
  - Upserts into ~/.hermes/brain/paper_portfolio.json
  - Status at ~/.hermes/brain/status/grid_eth_usdc.json
  - Per-sleeve grid state cached in ~/.hermes/brain/state/grid_eth_usdc_state.json

Paper only. R-001 compliant (Binance public spot endpoints, no key).
"""
from __future__ import annotations
import json
import logging
import os
import statistics
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

WORKER_NAME = "grid_eth_usdc"
SYMBOL = "ETHUSDC"

HERMES = Path.home() / ".hermes" / "brain"
PORTFOLIO_FILE = HERMES / "paper_portfolio.json"
STATUS_FILE = HERMES / "status" / "grid_eth_usdc.json"
STATE_FILE = HERMES / "state" / "grid_eth_usdc_state.json"

# Per-sleeve grid budget (USD).
SLEEVE_TARGETS = {
    "fund_60_40_income.structural_grid": 250.00,
    "fund_75_25_balanced.structural_grid": 250.00,
    "fund_90_10_growth.aggressive_grid": 200.00,
}

# Grid geometry. Total levels = 2*GRID_HALF (half above pivot, half below).
GRID_HALF = 5                   # 5 buy levels + 5 sell levels per sleeve
GRID_BAND_PCT = 0.06            # ±6% from pivot (so each level is 1.2% apart)

BINANCE_PRICE_URL = f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}"
BINANCE_KLINES_URL = f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval=1h&limit=24"

UA = {"User-Agent": "hermes-grid-eth-usdc/1.0"}

log = logging.getLogger(WORKER_NAME)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def http_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def fetch_mark() -> float | None:
    try:
        d = http_json(BINANCE_PRICE_URL)
        return float(d["price"])
    except Exception as e:
        log.warning("mark fetch failed: %s", e)
        return None


def fetch_pivot() -> float | None:
    """Median of last 24 hourly closes — robust against single-candle spikes."""
    try:
        d = http_json(BINANCE_KLINES_URL)
        closes = [float(k[4]) for k in d if k and len(k) > 4]
        if not closes:
            return None
        return statistics.median(closes)
    except Exception as e:
        log.warning("pivot fetch failed: %s", e)
        return None


def build_levels(pivot: float) -> list[float]:
    """Return 2*GRID_HALF level prices spanning ±GRID_BAND_PCT around pivot."""
    step = (GRID_BAND_PCT * pivot) / GRID_HALF
    levels = []
    for i in range(-GRID_HALF, GRID_HALF + 1):
        if i == 0:
            continue  # skip pivot itself
        levels.append(round(pivot + i * step, 4))
    return sorted(levels)


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


def fire_fill(positions, sleeve_id, level: float, mark: float, side: str,
              size_usd: float, now_iso: str) -> dict:
    """Open a paper buy or sell at the grid level. Buys remain open until a
    matching sell at a higher level resolves them as a round-trip."""
    fund_id = sleeve_id.split(".", 1)[0]
    pos_id = f"grid_eth_usdc:{sleeve_id}:{side}:{level:.4f}:{int(time.time())}"
    pos = {
        "id": pos_id,
        "worker": WORKER_NAME,
        "fund": fund_id,
        "sleeve": sleeve_id,
        "symbol": SYMBOL,
        "direction": "BUY" if side == "buy" else "SELL",
        "side": side,
        "entry_price": level,
        "mark_price": mark,
        "size_usd": size_usd,
        "principal_usd": size_usd,
        "shares": size_usd / level,
        "pnl_usd": 0.0,
        "entry_time": now_iso,
        "last_mark_time": now_iso,
        "resolved": False,
        "grid_level": level,
    }
    positions.append(pos)
    return pos


def resolve_round_trip(buy_pos: dict, sell_level: float, mark: float, now_iso: str):
    """A sell crossed a level above an open buy — close the round trip."""
    shares = buy_pos.get("shares", 0)
    realized = round(shares * (sell_level - buy_pos["entry_price"]), 6)
    buy_pos["resolved"] = True
    buy_pos["resolve_time"] = now_iso
    buy_pos["exit_price"] = sell_level
    buy_pos["pnl_usd"] = realized
    buy_pos["correct"] = realized > 0
    buy_pos["resolve_reason"] = "grid_round_trip"


def step_sleeve(positions, state, sleeve_id, target_usd, mark, pivot, now_iso):
    """Run one grid cycle for one sleeve. Returns (opened, resolved)."""
    sleeve_state = state.setdefault(sleeve_id, {})
    levels = sleeve_state.get("levels")
    if not levels or sleeve_state.get("pivot") != pivot:
        levels = build_levels(pivot)
        sleeve_state["levels"] = levels
        sleeve_state["pivot"] = pivot
        log.info("[%s] (re)built grid: pivot=%.4f levels=%d band=±%.1f%%",
                 sleeve_id, pivot, len(levels), GRID_BAND_PCT * 100)

    last_mark = sleeve_state.get("last_mark", mark)
    sleeve_state["last_mark"] = mark
    slot_size = round(target_usd / (2 * GRID_HALF), 4)

    opened = 0
    resolved = 0

    # detect crossings between last_mark and mark
    lo, hi = (last_mark, mark) if last_mark <= mark else (mark, last_mark)
    crossed = [lvl for lvl in levels if lo < lvl < hi or last_mark == lvl == mark]

    going_up = mark > last_mark
    open_buys = [p for p in positions_for_sleeve(positions, sleeve_id) if p.get("side") == "buy"]
    open_buys.sort(key=lambda p: p["entry_price"])

    for lvl in (sorted(crossed) if going_up else sorted(crossed, reverse=True)):
        if going_up:
            # crossing up = sell signal. Match against the lowest open buy below this level.
            matched = None
            for b in open_buys:
                if b["entry_price"] < lvl and not b.get("resolved"):
                    matched = b
                    break
            if matched is not None:
                resolve_round_trip(matched, lvl, mark, now_iso)
                resolved += 1
                open_buys.remove(matched)
            else:
                # no open buy to close — record an unmatched sell as a marker
                fire_fill(positions, sleeve_id, lvl, mark, "sell", slot_size, now_iso)
                opened += 1
        else:
            # crossing down = buy signal. Open a paper buy at this level.
            fire_fill(positions, sleeve_id, lvl, mark, "buy", slot_size, now_iso)
            opened += 1

    return opened, resolved


def write_status(per_sleeve_open, mark, pivot, ok, error_msg=None):
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).astimezone().isoformat()
    all_open = [p for ps in per_sleeve_open.values() for p in ps]
    deployed = sum(p.get("size_usd", 0) for p in all_open)
    status = {
        "worker_name": WORKER_NAME,
        "strategy_type": "spot_grid",
        "status": "active" if ok else "degraded",
        "last_heartbeat": now_iso,
        "cycle_count": 1,
        "position_summary": {
            "open_positions": len(all_open),
            "total_capital_deployed_usd": round(deployed, 4),
            "by_sleeve": {
                sid: {
                    "open_positions": len(ps),
                    "deployed_usd": round(sum(p.get("size_usd", 0) for p in ps), 4),
                }
                for sid, ps in per_sleeve_open.items()
            },
        },
        "performance": {
            "current_mark": mark,
            "pivot": pivot,
        },
        "risk": {
            "position_sizing_method": "per_sleeve_target",
            "sleeve_targets_usd": SLEEVE_TARGETS,
            "grid_half": GRID_HALF,
            "grid_band_pct": GRID_BAND_PCT,
            "counterparty": "binance_spot_public",
        },
        "strategy_config": {
            "exchange": "binance",
            "symbol": SYMBOL,
            "grid_band_pct": GRID_BAND_PCT,
            "grid_levels": 2 * GRID_HALF,
            "fund_sleeves": list(SLEEVE_TARGETS.keys()),
        },
        "errors_last_24h": 0 if ok else 1,
        "health_check": "green" if ok else "yellow",
    }
    if error_msg:
        status["last_error"] = error_msg
    save_json_atomic(STATUS_FILE, status)


def run_once():
    mark = fetch_mark()
    pivot = fetch_pivot()
    if mark is None or pivot is None:
        write_status({sid: [] for sid in SLEEVE_TARGETS}, mark, pivot,
                     ok=False, error_msg="binance fetch failed")
        return

    portfolio = load_json(PORTFOLIO_FILE, {"positions": []})
    positions = portfolio.get("positions", []) if isinstance(portfolio, dict) else portfolio
    state = load_json(STATE_FILE, {})
    now_iso = datetime.now(timezone.utc).isoformat()

    per_sleeve_open = {}
    total_opened = 0
    total_resolved = 0
    for sleeve_id, target in SLEEVE_TARGETS.items():
        opened, resolved = step_sleeve(positions, state, sleeve_id, target, mark, pivot, now_iso)
        total_opened += opened
        total_resolved += resolved
        per_sleeve_open[sleeve_id] = positions_for_sleeve(positions, sleeve_id)

    if isinstance(portfolio, dict):
        portfolio["positions"] = positions
        save_json_atomic(PORTFOLIO_FILE, portfolio)
    else:
        save_json_atomic(PORTFOLIO_FILE, positions)
    save_json_atomic(STATE_FILE, state)

    write_status(per_sleeve_open, mark, pivot, ok=True)
    print(f"[{WORKER_NAME}] mark={mark:.4f} pivot={pivot:.4f} sleeves={len(per_sleeve_open)} "
          f"opened={total_opened} resolved={total_resolved}")


if __name__ == "__main__":
    run_once()
