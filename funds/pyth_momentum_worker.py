#!/usr/bin/env python3
"""
pyth_momentum_worker — paper momentum scanner backed by Pyth Network oracle
prices (Hermes Price Service, free public endpoint).

Pairs with polymarket_btc_updown in fund_75_25_balanced.directional. Pyth
covers a wider symbol universe than Binance perps alone and is the
go-to oracle on Solana + L2s, so directional signals derived from Pyth
prices cover a different slice of the market than Binance-only workers.

Mechanism:
  - Universe: BTC/USD, ETH/USD, SOL/USD, LINK/USD from Pyth's crypto feed
  - Entry: 30-minute EMA cross vs 120-minute EMA (fast-above-slow = LONG)
  - Exit: reversal (slow-above-fast) OR -6% stop
  - Principal: per-sleeve target / universe_size (even-weight basket)
  - Positions tagged worker='pyth_momentum', fund=<fund_id>, sleeve=<sleeve_id>

Data source: https://hermes.pyth.network/api/latest_price_feeds
  Uses the "price" field from Pyth's signed JSON price feeds (no key).
Historical EMA is computed from Pyth's benchmarks endpoint:
  https://benchmarks.pyth.network/v1/shims/tradingview/history
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from engine.policy import sleeve_targets_for, worker_cfg

WORKER_NAME = "pyth_momentum"

HERMES = Path.home() / ".hermes" / "brain"
PORTFOLIO_FILE = HERMES / "paper_portfolio.json"
STATUS_FILE = HERMES / "status" / f"{WORKER_NAME}.json"
STATE_FILE = HERMES / "state" / f"{WORKER_NAME}_state.json"

# Pyth price feed IDs (see https://pyth.network/developers/price-feed-ids)
UNIVERSE = [
    {
        "symbol": "BTC",
        "feed_id": "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
        "tv": "Crypto.BTC/USD",
    },
    {
        "symbol": "ETH",
        "feed_id": "ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
        "tv": "Crypto.ETH/USD",
    },
    {
        "symbol": "SOL",
        "feed_id": "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
        "tv": "Crypto.SOL/USD",
    },
    {
        "symbol": "LINK",
        "feed_id": "8ac0c70fff57e9aefdf5edf44b51d62c2d433653cbb2cf5cc06bb115af04d221",
        "tv": "Crypto.LINK/USD",
    },
]

_FALLBACK_TARGETS = {"fund_75_25_balanced.directional": 100.00}
SLEEVE_TARGETS = sleeve_targets_for(WORKER_NAME) or _FALLBACK_TARGETS

_cfg = worker_cfg(WORKER_NAME)
FAST_EMA_MINUTES = _cfg.get("fast_ema_minutes", 30)
SLOW_EMA_MINUTES = _cfg.get("slow_ema_minutes", 120)
ENTRY_GAP_PCT = _cfg.get("entry_gap_pct", 0.25)
EXIT_GAP_PCT = _cfg.get("exit_gap_pct", -0.25)
STOP_LOSS_PCT = _cfg.get("stop_loss_pct", -6.0)

HERMES_LATEST = "https://hermes.pyth.network/api/latest_price_feeds"
PYTH_HISTORY = "https://benchmarks.pyth.network/v1/shims/tradingview/history"

log = logging.getLogger(WORKER_NAME)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

UA = {"User-Agent": "hermes-pyth-momentum/1.0"}


def _http_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def fetch_latest_prices() -> dict[str, float]:
    params = "&".join(f"ids[]={item['feed_id']}" for item in UNIVERSE)
    url = f"{HERMES_LATEST}?{params}"
    try:
        feeds = _http_json(url)
    except Exception as e:
        log.warning("hermes latest failed: %s", e)
        return {}
    out = {}
    by_id = {item["feed_id"]: item["symbol"] for item in UNIVERSE}
    for f in feeds:
        fid = (f.get("id") or "").lower()
        if fid not in by_id:
            continue
        price_obj = f.get("price", {})
        try:
            mantissa = int(price_obj["price"])
            expo = int(price_obj["expo"])
            out[by_id[fid]] = mantissa * (10**expo)
        except (KeyError, ValueError):
            continue
    return out


def fetch_history_closes(tv_symbol: str, minutes_back: int) -> list[float]:
    now = int(time.time())
    params = {
        "symbol": tv_symbol,
        "resolution": "1",
        "from": now - minutes_back * 60,
        "to": now,
    }
    url = f"{PYTH_HISTORY}?{urllib.parse.urlencode(params)}"
    try:
        d = _http_json(url)
    except Exception as e:
        log.warning("history fetch failed for %s: %s", tv_symbol, e)
        return []
    closes = d.get("c") or []
    return [float(c) for c in closes if c]


def ema(values: list[float], window: int) -> float | None:
    if not values or len(values) < window:
        return None
    k = 2 / (window + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


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
    now_iso = datetime.now(UTC).isoformat()

    marks = fetch_latest_prices()
    if not marks:
        log.warning("no pyth prices; skipping cycle")
        save_json_atomic(
            STATUS_FILE,
            {
                "worker_name": WORKER_NAME,
                "status": "degraded",
                "last_heartbeat": now_iso,
                "errors_last_24h": 1,
                "last_error": "hermes latest_price_feeds returned empty",
            },
        )
        return

    opened = 0
    resolved = 0
    per_sleeve_state = {}

    for sleeve_id, target_usd in SLEEVE_TARGETS.items():
        fund_id = sleeve_id.split(".", 1)[0]
        slot_size = round(target_usd / len(UNIVERSE), 4)
        open_pos = positions_for_sleeve(positions, sleeve_id)
        open_by_symbol = {p.get("symbol"): p for p in open_pos}

        per_sleeve_state[sleeve_id] = {"open": 0, "deployed": 0.0}

        for item in UNIVERSE:
            sym = item["symbol"]
            mark = marks.get(sym)
            if mark is None or mark <= 0:
                continue
            closes = fetch_history_closes(item["tv"], SLOW_EMA_MINUTES + 5)
            if len(closes) < SLOW_EMA_MINUTES:
                continue
            fast = ema(closes[-FAST_EMA_MINUTES:], FAST_EMA_MINUTES)
            slow = ema(closes, SLOW_EMA_MINUTES)
            if fast is None or slow is None or slow <= 0:
                continue
            gap_pct = ((fast - slow) / slow) * 100.0

            existing = open_by_symbol.get(sym)
            if existing is None:
                if gap_pct < ENTRY_GAP_PCT:
                    continue
                pos = {
                    "id": f"pyth_momentum:{sleeve_id}:{sym}:{int(time.time())}",
                    "worker": WORKER_NAME,
                    "fund": fund_id,
                    "sleeve": sleeve_id,
                    "symbol": sym,
                    "direction": "long",
                    "entry_price": mark,
                    "mark_price": mark,
                    "principal_usd": slot_size,
                    "size_usd": slot_size,
                    "pnl_usd": 0.0,
                    "entry_time": now_iso,
                    "last_mark_time": now_iso,
                    "entry_fast_ema": round(fast, 6),
                    "entry_slow_ema": round(slow, 6),
                    "entry_gap_pct": round(gap_pct, 3),
                    "resolved": False,
                }
                positions.append(pos)
                open_by_symbol[sym] = pos
                opened += 1
                continue

            entry = existing.get("entry_price") or mark
            pnl_pct = ((mark - entry) / entry) * 100.0 if entry else 0.0
            shares = existing.get("principal_usd", slot_size) / entry if entry else 0.0
            existing["mark_price"] = mark
            existing["last_mark_time"] = now_iso
            existing["size_usd"] = round(shares * mark, 4)
            existing["pnl_usd"] = round(existing["size_usd"] - existing["principal_usd"], 4)
            existing["current_gap_pct"] = round(gap_pct, 3)

            if gap_pct <= EXIT_GAP_PCT:
                existing["resolved"] = True
                existing["resolve_time"] = now_iso
                existing["resolve_reason"] = f"ema_flip_{gap_pct:.2f}%"
                existing["correct"] = existing["pnl_usd"] > 0
                resolved += 1
            elif pnl_pct <= STOP_LOSS_PCT:
                existing["resolved"] = True
                existing["resolve_time"] = now_iso
                existing["resolve_reason"] = f"stop_loss_{pnl_pct:.2f}%"
                existing["correct"] = False
                resolved += 1

    if isinstance(portfolio, dict):
        portfolio["positions"] = positions
        save_json_atomic(PORTFOLIO_FILE, portfolio)
    else:
        save_json_atomic(PORTFOLIO_FILE, positions)

    all_open = [
        p
        for p in positions
        if isinstance(p, dict) and p.get("worker") == WORKER_NAME and not p.get("resolved")
    ]
    deployed = sum(p.get("size_usd", 0) for p in all_open)

    save_json_atomic(
        STATUS_FILE,
        {
            "worker_name": WORKER_NAME,
            "strategy_type": "momentum",
            "status": "active" if all_open else "scanning",
            "last_heartbeat": datetime.now().astimezone().isoformat(),
            "position_summary": {
                "open_positions": len(all_open),
                "total_capital_deployed_usd": round(deployed, 4),
            },
            "this_cycle": {"opened": opened, "resolved": resolved, "universe_size": len(UNIVERSE)},
            "risk": {
                "position_sizing_method": "per_sleeve_even_weight",
                "sleeve_targets_usd": SLEEVE_TARGETS,
                "fast_ema_minutes": FAST_EMA_MINUTES,
                "slow_ema_minutes": SLOW_EMA_MINUTES,
                "entry_gap_pct": ENTRY_GAP_PCT,
                "stop_loss_pct": STOP_LOSS_PCT,
                "counterparty": "pyth_oracle_network",
            },
            "strategy_config": {
                "source": "pyth_hermes_public",
                "fund_sleeves": list(SLEEVE_TARGETS.keys()),
            },
            "errors_last_24h": 0,
            "health_check": "green",
        },
    )

    print(
        f"[{WORKER_NAME}] sleeves={len(SLEEVE_TARGETS)} open={len(all_open)} "
        f"deployed=${deployed:.2f} opened={opened} resolved={resolved}"
    )


if __name__ == "__main__":
    run_once()
