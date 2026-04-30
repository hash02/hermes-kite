#!/usr/bin/env python3
"""
Shared engine for spot grid workers.

Each grid worker (grid_eth_usdc, grid_btc_usdc, grid_sol, grid_stables)
supplies a GridConfig and calls run_grid(cfg). All shared mechanics —
pivot fetch, level generation, cycle stepping, round-trip resolution,
portfolio persistence, status emission — live here.

Not a worker on its own. Import-only.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

HERMES = Path.home() / ".hermes" / "brain"
PORTFOLIO_FILE = HERMES / "paper_portfolio.json"

UA = {"User-Agent": "hermes-grid-base/1.0"}


@dataclass
class GridConfig:
    worker_name: str
    symbol: str  # Display symbol (e.g. "ETHUSDC", "USDCUSDT")
    price_url: str  # JSON endpoint returning {"price": "..."}
    klines_url: str  # JSON endpoint returning list of kline rows
    sleeve_targets: dict  # {"fund_xxx.sleeve": usd_target}
    grid_half: int = 5  # 5 levels each side of pivot
    grid_band_pct: float = 0.06  # ±6% band
    price_field_parser: Callable[[dict], float] | None = None
    kline_close_parser: Callable[[list], float] | None = None
    status_file: Path = field(init=False)
    state_file: Path = field(init=False)

    def __post_init__(self):
        self.status_file = HERMES / "status" / f"{self.worker_name}.json"
        self.state_file = HERMES / "state" / f"{self.worker_name}_state.json"


# ---------- fetch ----------


def _http_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def fetch_mark(cfg: GridConfig, log) -> float | None:
    try:
        d = _http_json(cfg.price_url)
        if cfg.price_field_parser:
            return cfg.price_field_parser(d)
        return float(d["price"])
    except Exception as e:
        log.warning("mark fetch failed: %s", e)
        return None


def fetch_pivot(cfg: GridConfig, log) -> float | None:
    try:
        d = _http_json(cfg.klines_url)
        if cfg.kline_close_parser:
            closes = [cfg.kline_close_parser(k) for k in d if k]
        else:
            closes = [float(k[4]) for k in d if k and len(k) > 4]
        closes = [c for c in closes if c is not None and c > 0]
        if not closes:
            return None
        return statistics.median(closes)
    except Exception as e:
        log.warning("pivot fetch failed: %s", e)
        return None


# ---------- grid geometry ----------


def build_levels(pivot: float, grid_half: int, band_pct: float) -> list[float]:
    step = (band_pct * pivot) / grid_half
    levels = []
    for i in range(-grid_half, grid_half + 1):
        if i == 0:
            continue
        levels.append(round(pivot + i * step, 6))
    return sorted(levels)


# ---------- portfolio plumbing ----------


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


def _load_json(p: Path, default):
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _save_json_atomic(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(p)


def positions_for_sleeve(positions, worker_name, sleeve_id, open_only=True):
    out = []
    for p in positions:
        if not isinstance(p, dict):
            continue
        if p.get("worker") != worker_name:
            continue
        if p.get("sleeve") != sleeve_id:
            continue
        if open_only and p.get("resolved"):
            continue
        out.append(p)
    return out


# ---------- fill + resolve ----------


def _fire_fill(positions, cfg, sleeve_id, level, mark, side, size_usd, now_iso):
    fund_id = sleeve_id.split(".", 1)[0]
    pos_id = f"{cfg.worker_name}:{sleeve_id}:{side}:{level:.6f}:{int(time.time() * 1000)}"
    pos = {
        "id": pos_id,
        "worker": cfg.worker_name,
        "fund": fund_id,
        "sleeve": sleeve_id,
        "symbol": cfg.symbol,
        "direction": "BUY" if side == "buy" else "SELL",
        "side": side,
        "entry_price": level,
        "mark_price": mark,
        "size_usd": size_usd,
        "principal_usd": size_usd,
        "shares": size_usd / level if level > 0 else 0,
        "pnl_usd": 0.0,
        "entry_time": now_iso,
        "last_mark_time": now_iso,
        "resolved": False,
        "grid_level": level,
    }
    positions.append(pos)
    return pos


def _resolve_round_trip(buy_pos, sell_level, now_iso):
    shares = buy_pos.get("shares", 0)
    realized = round(shares * (sell_level - buy_pos["entry_price"]), 6)
    buy_pos["resolved"] = True
    buy_pos["resolve_time"] = now_iso
    buy_pos["exit_price"] = sell_level
    buy_pos["pnl_usd"] = realized
    buy_pos["correct"] = realized > 0
    buy_pos["resolve_reason"] = "grid_round_trip"


def step_sleeve(positions, state, cfg: GridConfig, sleeve_id, target_usd, mark, pivot, now_iso, log):
    sleeve_state = state.setdefault(sleeve_id, {})
    levels = sleeve_state.get("levels")
    if not levels or sleeve_state.get("pivot") != pivot:
        levels = build_levels(pivot, cfg.grid_half, cfg.grid_band_pct)
        sleeve_state["levels"] = levels
        sleeve_state["pivot"] = pivot
        log.info(
            "[%s] grid (re)built pivot=%.6f levels=%d band=±%.1f%%",
            sleeve_id,
            pivot,
            len(levels),
            cfg.grid_band_pct * 100,
        )

    last_mark = sleeve_state.get("last_mark", mark)
    sleeve_state["last_mark"] = mark
    slot_size = round(target_usd / (2 * cfg.grid_half), 6)

    opened = 0
    resolved = 0
    lo, hi = (last_mark, mark) if last_mark <= mark else (mark, last_mark)
    crossed = [lvl for lvl in levels if lo < lvl < hi or last_mark == lvl == mark]
    going_up = mark > last_mark

    open_buys = [
        p for p in positions_for_sleeve(positions, cfg.worker_name, sleeve_id) if p.get("side") == "buy"
    ]
    open_buys.sort(key=lambda p: p["entry_price"])

    order = sorted(crossed) if going_up else sorted(crossed, reverse=True)
    for lvl in order:
        if going_up:
            matched = None
            for b in open_buys:
                if b["entry_price"] < lvl and not b.get("resolved"):
                    matched = b
                    break
            if matched is not None:
                _resolve_round_trip(matched, lvl, now_iso)
                resolved += 1
                open_buys.remove(matched)
            else:
                _fire_fill(positions, cfg, sleeve_id, lvl, mark, "sell", slot_size, now_iso)
                opened += 1
        else:
            _fire_fill(positions, cfg, sleeve_id, lvl, mark, "buy", slot_size, now_iso)
            opened += 1

    return opened, resolved


def write_status(cfg: GridConfig, per_sleeve_open, mark, pivot, ok, error_msg: str | None = None):
    now_iso = datetime.now(UTC).astimezone().isoformat()
    all_open = [p for ps in per_sleeve_open.values() for p in ps]
    deployed = sum(p.get("size_usd", 0) for p in all_open)
    status = {
        "worker_name": cfg.worker_name,
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
            "sleeve_targets_usd": cfg.sleeve_targets,
            "grid_half": cfg.grid_half,
            "grid_band_pct": cfg.grid_band_pct,
            "counterparty": "public_spot",
        },
        "strategy_config": {
            "symbol": cfg.symbol,
            "fund_sleeves": list(cfg.sleeve_targets.keys()),
        },
        "errors_last_24h": 0 if ok else 1,
        "health_check": "green" if ok else "yellow",
    }
    if error_msg:
        status["last_error"] = error_msg
    _save_json_atomic(cfg.status_file, status)


def run_grid(cfg: GridConfig):
    log = logging.getLogger(cfg.worker_name)
    if not log.handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    mark = fetch_mark(cfg, log)
    pivot = fetch_pivot(cfg, log)
    if mark is None or pivot is None:
        write_status(
            cfg, {sid: [] for sid in cfg.sleeve_targets}, mark, pivot, ok=False, error_msg="feed fetch failed"
        )
        return

    portfolio = load_portfolio()
    positions = portfolio.get("positions", []) if isinstance(portfolio, dict) else portfolio
    state = _load_json(cfg.state_file, {})
    now_iso = datetime.now(UTC).isoformat()

    per_sleeve_open = {}
    total_opened = 0
    total_resolved = 0
    for sleeve_id, target in cfg.sleeve_targets.items():
        opened, resolved = step_sleeve(positions, state, cfg, sleeve_id, target, mark, pivot, now_iso, log)
        total_opened += opened
        total_resolved += resolved
        per_sleeve_open[sleeve_id] = positions_for_sleeve(positions, cfg.worker_name, sleeve_id)

    if isinstance(portfolio, dict):
        portfolio["positions"] = positions
        save_portfolio_atomic(portfolio)
    else:
        save_portfolio_atomic(positions)
    _save_json_atomic(cfg.state_file, state)

    write_status(cfg, per_sleeve_open, mark, pivot, ok=True)
    print(
        f"[{cfg.worker_name}] mark={mark:.6f} pivot={pivot:.6f} "
        f"sleeves={len(per_sleeve_open)} opened={total_opened} resolved={total_resolved}"
    )
