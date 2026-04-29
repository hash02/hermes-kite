#!/usr/bin/env python3
"""
Shared engine for lending-style yield workers.

aave_usdc, morpho_usdc, sgho, euler_pyusd, superstate_uscc — each one
supplies/holds a single fixed-yield asset and accrues paper interest
at a live APY pulled from a public source. All share identical mechanics:
upsert one fund-scoped position per sleeve, accrue principal * apy * dt,
write status.

This module extracts the mechanics. Existing workers keep their own
implementations for now (stable, already tested); new workers built on
this module stay short and consistent.

Not a worker on its own. Import-only.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

HERMES = Path.home() / ".hermes" / "brain"
PORTFOLIO_FILE = HERMES / "paper_portfolio.json"

UA = {"User-Agent": "hermes-yield-base/1.0"}


@dataclass
class YieldConfig:
    worker_name: str
    symbol: str
    sleeve_targets: dict  # {"fund_xxx.sleeve": usd_principal}
    # Either an APY fetcher callable, or a DeFiLlama pool_id to use the default.
    defillama_pool_id: str | None = None
    apy_fetcher: Callable[[], tuple[float | None, str]] | None = None
    # Metadata for status emission
    protocol: str = ""
    chain: str = "ethereum"
    asset: str = ""
    counterparty: str = ""
    direction: str = "SUPPLY"  # SUPPLY / HOLD / VAULT
    confidence: float = 0.92
    status_file: Path = field(init=False)

    def __post_init__(self):
        self.status_file = HERMES / "status" / f"{self.worker_name}.json"


def _defillama_apy(pool_id: str):
    url = f"https://yields.llama.fi/chart/{pool_id}"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    points = data.get("data", [])
    if not points:
        return None, ""
    latest = points[-1]
    apy_pct = latest.get("apyBase") or latest.get("apy")
    if apy_pct is None:
        return None, ""
    return float(apy_pct) / 100.0, str(latest.get("timestamp", ""))


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
    except (OSError, json.JSONDecodeError):
        return {"positions": [], "realized_pnl": 0.0}


def save_portfolio_atomic(pf):
    tmp = PORTFOLIO_FILE.with_suffix(".json.tmp." + str(os.getpid()))
    tmp.write_text(json.dumps(pf, indent=2))
    tmp.replace(PORTFOLIO_FILE)


def _position_id(worker_name: str, sleeve_id: str) -> str:
    return f"{worker_name}:{sleeve_id}"


def _upsert_sleeve_position(pf, cfg: YieldConfig, sleeve_id: str, principal: float, apy):
    now = time.time()
    fund_id = sleeve_id.split(".", 1)[0]
    pos_id = _position_id(cfg.worker_name, sleeve_id)
    positions = pf.get("positions", [])
    if not isinstance(positions, list):
        positions = []
    existing = None
    for p in positions:
        if isinstance(p, dict) and p.get("worker") == cfg.worker_name and p.get("id") == pos_id:
            existing = p
            break
    if existing is None:
        position = {
            "id": pos_id,
            "worker": cfg.worker_name,
            "fund": fund_id,
            "sleeve": sleeve_id,
            "symbol": cfg.symbol,
            "direction": cfg.direction,
            "entry_price": 1.00,
            "size_usd": principal,
            "principal_usd": principal,
            "confidence": cfg.confidence,
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
            existing["high_water_mark"] = max(
                existing.get("high_water_mark", principal), existing["size_usd"]
            )
            existing["low_water_mark"] = min(
                existing.get("low_water_mark", principal), existing["size_usd"]
            )
        existing["current_apy"] = apy if apy is not None else existing.get("current_apy", 0.0)
        existing["last_update"] = now
        existing["fund"] = fund_id
        existing["sleeve"] = sleeve_id

    pf["positions"] = positions
    return existing


def _write_status(cfg: YieldConfig, positions, apy, ok, error_msg=None):
    cfg.status_file.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(UTC).astimezone().isoformat()
    total_size = sum(p.get("size_usd", 0) for p in positions)
    total_pnl = sum(p.get("pnl_usd", 0) for p in positions)
    total_principal = sum(p.get("principal_usd", 0) for p in positions)
    status = {
        "worker_name": cfg.worker_name,
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
            "counterparty": cfg.counterparty,
            "protocol_risk": f"smart_contract_{cfg.protocol}",
        },
        "strategy_config": {
            "protocol": cfg.protocol,
            "chain": cfg.chain,
            "asset": cfg.asset,
            "pool_id": cfg.defillama_pool_id,
            "data_source": "defillama_free" if cfg.defillama_pool_id else "custom",
            "sleeve_targets_usd": cfg.sleeve_targets,
            "fund_sleeves": list(cfg.sleeve_targets.keys()),
        },
        "errors_last_24h": 0 if ok else 1,
        "health_check": "green" if ok else "yellow",
    }
    if error_msg:
        status["last_error"] = error_msg
    cfg.status_file.write_text(json.dumps(status, indent=2))


def run_yield(cfg: YieldConfig):
    apy = None
    ok = True
    err = None
    fetcher = cfg.apy_fetcher
    if fetcher is None and cfg.defillama_pool_id:
        pool_id = cfg.defillama_pool_id

        def fetcher():
            return _defillama_apy(pool_id)

    if fetcher is None:
        ok = False
        err = "no apy source configured"
    else:
        try:
            apy, _ = fetcher()
            if apy is None:
                ok = False
                err = "APY fetch returned no data"
        except Exception as e:
            ok = False
            err = f"fetch failed: {e}"

    pf = load_portfolio()
    positions = []
    for sleeve_id, principal in cfg.sleeve_targets.items():
        positions.append(_upsert_sleeve_position(pf, cfg, sleeve_id, principal, apy))
    save_portfolio_atomic(pf)
    _write_status(cfg, positions, apy, ok, err)

    apy_str = f"{apy * 100:.4f}%" if apy is not None else "UNK"
    total_size = sum(p.get("size_usd", 0) for p in positions)
    total_pnl = sum(p.get("pnl_usd", 0) for p in positions)
    print(
        f"[{cfg.worker_name}] apy={apy_str}  sleeves={len(positions)}  "
        f"total_size=${total_size:.6f}  total_pnl=${total_pnl:.6f}  ok={ok}"
    )
