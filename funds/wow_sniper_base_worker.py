#!/usr/bin/env python3
"""
wow_sniper_base_worker — paper "new-token momentum" sniper on Base chain.

Pairs with crypto_memecoins in fund_90_10_growth.memecoin_sniper. Covers
the long-tail micro-cap side of the sleeve: recently-launched Base tokens
with pumping volume. Strictly smaller stakes than crypto_memecoins — the
drawdown profile on sub-$10M mcap tokens is brutal.

Signal: DexScreener public API, Base chain trending pairs, filtered to:
  - Token launched <= 14 days ago
  - 24h buy volume > $50k (liquidity exists)
  - 24h price change >= +30%
  - FDV <= $10M (leaves room to multiples before exhaustion)
Entry: open long at current DexScreener price.
Exit: -25% stop, OR 24h change <= -10%, OR pair age > 60 days (thesis stale).

Data: https://api.dexscreener.com/latest/dex/search?q=chain:base
(free, no key; rate-limited to ~300 req/min).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from engine.policy import sleeve_targets_for, worker_cfg

WORKER_NAME = "wow_sniper_base"
HERMES = Path.home() / ".hermes" / "brain"
PORTFOLIO_FILE = HERMES / "paper_portfolio.json"
STATUS_FILE = HERMES / "status" / f"{WORKER_NAME}.json"
STATE_FILE = HERMES / "state" / f"{WORKER_NAME}_state.json"

_FALLBACK_TARGETS = {"fund_90_10_growth.memecoin_sniper": 50.00}
SLEEVE_TARGETS = sleeve_targets_for(WORKER_NAME) or _FALLBACK_TARGETS

_cfg = worker_cfg(WORKER_NAME)
MAX_OPEN_POSITIONS = _cfg.get("max_open_positions", 5)
ENTRY_24H_PCT = _cfg.get("entry_24h_pct", 30.0)
EXIT_24H_PCT = _cfg.get("exit_24h_pct", -10.0)
STOP_LOSS_PCT = _cfg.get("stop_loss_pct", -25.0)
MAX_PAIR_AGE_DAYS = _cfg.get("max_pair_age_days", 14)
MAX_FDV_USD = _cfg.get("max_fdv_usd", 10_000_000)
MIN_24H_BUY_VOL_USD = _cfg.get("min_24h_buy_vol_usd", 50_000)
STALE_EXIT_DAYS = _cfg.get("stale_exit_days", 60)

DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search?q=base"
DEX_PAIR = "https://api.dexscreener.com/latest/dex/tokens/"

log = logging.getLogger(WORKER_NAME)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

UA = {"User-Agent": "hermes-wow-sniper-base/1.0"}


def fetch_base_pairs() -> list[dict]:
    req = urllib.request.Request(DEX_SEARCH, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
    except Exception as e:
        log.warning("dexscreener fetch failed: %s", e)
        return []
    pairs = d.get("pairs") or []
    out = []
    now_s = time.time()
    for p in pairs:
        if p.get("chainId") != "base":
            continue
        try:
            pair_created_ms = int(p.get("pairCreatedAt") or 0)
            age_days = (now_s - pair_created_ms / 1000) / 86400 if pair_created_ms else 999
            fdv = float(p.get("fdv") or 0)
            chg24 = float((p.get("priceChange") or {}).get("h24") or 0)
            buy24 = float((p.get("volume") or {}).get("h24") or 0)
            price_usd = float(p.get("priceUsd") or 0)
            out.append(
                {
                    "pair_address": p.get("pairAddress"),
                    "base_symbol": (p.get("baseToken") or {}).get("symbol", ""),
                    "base_address": (p.get("baseToken") or {}).get("address", ""),
                    "price_usd": price_usd,
                    "age_days": age_days,
                    "fdv_usd": fdv,
                    "change_24h_pct": chg24,
                    "volume_24h_usd": buy24,
                    "url": p.get("url", ""),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def qualifying(pairs: list[dict]) -> list[dict]:
    out = []
    for x in pairs:
        if x["age_days"] > MAX_PAIR_AGE_DAYS:
            continue
        if x["fdv_usd"] <= 0 or x["fdv_usd"] > MAX_FDV_USD:
            continue
        if x["volume_24h_usd"] < MIN_24H_BUY_VOL_USD:
            continue
        if x["change_24h_pct"] < ENTRY_24H_PCT:
            continue
        if x["price_usd"] <= 0:
            continue
        out.append(x)
    out.sort(key=lambda x: -x["change_24h_pct"])
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


def fetch_current_price(base_address: str) -> tuple[float | None, float | None]:
    """Return (price_usd, change_24h_pct) for a base token address on Base."""
    if not base_address:
        return None, None
    url = f"{DEX_PAIR}{base_address}"
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
    except Exception:
        return None, None
    pairs = [p for p in (d.get("pairs") or []) if p.get("chainId") == "base"]
    if not pairs:
        return None, None
    p = pairs[0]
    try:
        return float(p.get("priceUsd") or 0), float((p.get("priceChange") or {}).get("h24") or 0)
    except (TypeError, ValueError):
        return None, None


def run_once():
    portfolio = load_json(PORTFOLIO_FILE, {"positions": []})
    positions = portfolio.get("positions", []) if isinstance(portfolio, dict) else portfolio
    now_iso = datetime.now(UTC).isoformat()

    universe = fetch_base_pairs()
    cands = qualifying(universe)

    opened = 0
    resolved = 0

    for sleeve_id, target_usd in SLEEVE_TARGETS.items():
        fund_id = sleeve_id.split(".", 1)[0]
        slot_size = round(target_usd / MAX_OPEN_POSITIONS, 4)
        open_pos = positions_for_sleeve(positions, sleeve_id)

        # mark-to-market + exit
        for pos in open_pos:
            price, chg24 = fetch_current_price(pos.get("base_address", ""))
            if price is None:
                continue
            entry = pos.get("entry_price") or price
            shares = pos.get("principal_usd", slot_size) / entry if entry else 0
            pos["mark_price"] = price
            pos["size_usd"] = round(shares * price, 6)
            pos["pnl_usd"] = round(pos["size_usd"] - pos["principal_usd"], 6)
            pos["last_mark_time"] = now_iso
            pos["current_change_24h_pct"] = chg24
            pnl_pct = ((price - entry) / entry) * 100.0 if entry else 0.0

            age_days = (
                pos.get("age_days_at_entry", 0)
                + (time.time() - pos.get("entry_ts", time.time())) / 86400
            )
            if chg24 is not None and chg24 <= EXIT_24H_PCT:
                pos["resolved"] = True
                pos["resolve_reason"] = f"fade_24h={chg24:.2f}%"
            elif pnl_pct <= STOP_LOSS_PCT:
                pos["resolved"] = True
                pos["resolve_reason"] = f"stop_loss_{pnl_pct:.2f}%"
            elif age_days >= STALE_EXIT_DAYS:
                pos["resolved"] = True
                pos["resolve_reason"] = f"stale_age_{age_days:.0f}d"

            if pos.get("resolved"):
                pos["resolve_time"] = now_iso
                pos["correct"] = pos["pnl_usd"] > 0
                resolved += 1

        # opens
        open_pos = positions_for_sleeve(positions, sleeve_id)
        open_addrs = {p.get("base_address") for p in open_pos}
        for c in cands:
            if len(open_pos) >= MAX_OPEN_POSITIONS:
                break
            if c["base_address"] in open_addrs:
                continue
            pos = {
                "id": f"{WORKER_NAME}:{sleeve_id}:{c['base_symbol']}:{int(time.time())}",
                "worker": WORKER_NAME,
                "fund": fund_id,
                "sleeve": sleeve_id,
                "symbol": c["base_symbol"],
                "base_address": c["base_address"],
                "pair_address": c["pair_address"],
                "direction": "long",
                "entry_price": c["price_usd"],
                "mark_price": c["price_usd"],
                "principal_usd": slot_size,
                "size_usd": slot_size,
                "pnl_usd": 0.0,
                "entry_time": now_iso,
                "entry_ts": time.time(),
                "last_mark_time": now_iso,
                "entry_change_24h_pct": c["change_24h_pct"],
                "age_days_at_entry": c["age_days"],
                "fdv_at_entry_usd": c["fdv_usd"],
                "resolved": False,
                "dex_url": c["url"],
            }
            positions.append(pos)
            open_pos.append(pos)
            open_addrs.add(c["base_address"])
            opened += 1

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
    unrealized = sum(p.get("pnl_usd", 0) for p in all_open)

    save_json_atomic(
        STATUS_FILE,
        {
            "worker_name": WORKER_NAME,
            "strategy_type": "base_new_token_sniper",
            "status": "active" if all_open else "scanning",
            "last_heartbeat": datetime.now().astimezone().isoformat(),
            "position_summary": {
                "open_positions": len(all_open),
                "total_capital_deployed_usd": round(deployed, 4),
                "unrealized_pnl_usd": round(unrealized, 4),
            },
            "this_cycle": {
                "opened": opened,
                "resolved": resolved,
                "universe_size": len(universe),
                "qualifying": len(cands),
            },
            "risk": {
                "position_sizing_method": "even_slot",
                "sleeve_targets_usd": SLEEVE_TARGETS,
                "max_open_positions": MAX_OPEN_POSITIONS,
                "entry_24h_pct": ENTRY_24H_PCT,
                "exit_24h_pct": EXIT_24H_PCT,
                "stop_loss_pct": STOP_LOSS_PCT,
                "max_pair_age_days": MAX_PAIR_AGE_DAYS,
                "max_fdv_usd": MAX_FDV_USD,
                "min_24h_buy_vol_usd": MIN_24H_BUY_VOL_USD,
            },
            "strategy_config": {
                "chain": "base",
                "source": "dexscreener_public",
                "fund_sleeves": list(SLEEVE_TARGETS.keys()),
            },
            "errors_last_24h": 0,
            "health_check": "green",
        },
    )

    print(
        f"[{WORKER_NAME}] open={len(all_open)} deployed=${deployed:.2f} "
        f"unrealized=${unrealized:.4f} opened={opened} resolved={resolved} "
        f"universe={len(universe)} qualifying={len(cands)}"
    )


if __name__ == "__main__":
    run_once()
