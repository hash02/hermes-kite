#!/usr/bin/env python3
"""
Risk engine — derives per-sleeve position sizes dynamically from:

  1. Static policy targets        (config/policy.json)
  2. Realized vol per sleeve      (from resolved position history, else bootstrap)
  3. Target portfolio vol cap     (risk.target_portfolio_vol_pct)
  4. Kelly fraction scale         (risk.kelly_fraction, default 0.25 = quarter-Kelly)
  5. Fund drawdown halt           (risk.max_drawdown_halt_per_fund_pct)
  6. Counterparty concentration   (risk.max_concentration_per_counterparty_pct)

When `risk.engine_enabled = false` (default), the engine is a no-op:
`apply_engine(worker_name, static)` returns `static` unchanged. Flip the
flag in policy.json to activate dynamic sizing; the static values then
serve as boot config (used before enough history exists to estimate vol).

Math (summary):
  per_sleeve_vol_budget = target_portfolio_vol_pct / sqrt(n_sleeves_in_fund)
  vol_capped_usd        = fund_capital * per_sleeve_vol_budget / realized_vol_pct
  sized_usd             = min(static_target_usd, vol_capped_usd) * kelly_fraction
  sized_usd             = 0   if fund_drawdown_pct <= -max_drawdown_halt_per_fund_pct
  sized_usd            *= counterparty_concentration_scalar

CLI:
  python3 funds/risk_engine.py --show                 # print current sizes
  python3 funds/risk_engine.py --show --enable-preview  # show what sizes *would* be if enabled
  python3 funds/risk_engine.py --enable               # flip engine_enabled=true in policy
  python3 funds/risk_engine.py --disable              # flip engine_enabled=false
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_FILE = REPO_ROOT / "config" / "policy.json"
LIVE_PORTFOLIO = Path.home() / ".hermes" / "brain" / "paper_portfolio.json"
PORTFOLIO_SUMMARY = REPO_ROOT / "data" / "portfolio_summary.json"

# Bootstrap realized vol (annualized %, absolute) by strategy category.
# Used when there are no resolved positions yet to estimate from history.
# Numbers are pragmatic — conservative rather than optimistic.
_BOOTSTRAP_VOL_PCT = {
    "yield": 1.0,  # stablecoin lending — smart-contract risk dominates vol
    "delta_neutral": 3.0,  # funding-rate arb, risk mostly from unwind slippage
    "grid": 8.0,  # spot grid on ETH/BTC — vol of the underlying * grid fraction
    "grid_stables": 0.5,  # USDC/USDT pair rarely moves
    "directional": 25.0,  # single-asset directional crypto
    "momentum": 25.0,  # equivalent to directional
    "binary": 40.0,  # polymarket NO-longshot — binary resolution
    "tokenized_stock": 20.0,  # tokenized equity, similar to US blue chips
    "memecoin": 80.0,  # large-cap memecoin — fat-tail downside
    "sniper": 120.0,  # new-token sniper — asymmetric, brutal
    "default": 15.0,
}

# Classify each worker -> strategy category. Used for bootstrap vol lookup
# and counterparty attribution.
_WORKER_META = {
    "aave_usdc": {"category": "yield", "counterparty": "aave_v3"},
    "morpho_usdc": {"category": "yield", "counterparty": "morpho_blue"},
    "euler_pyusd": {"category": "yield", "counterparty": "euler_v2"},
    "sgho": {"category": "yield", "counterparty": "aave_savings"},
    "superstate_uscc": {"category": "yield", "counterparty": "superstate"},
    "delta_neutral_funding": {"category": "delta_neutral", "counterparty": "binance_perp"},
    "polymarket_btc_updown": {"category": "binary", "counterparty": "polymarket"},
    "pyth_momentum": {"category": "directional", "counterparty": "pyth_oracle"},
    "grid_eth_usdc": {"category": "grid", "counterparty": "binance_spot"},
    "grid_btc_usdc": {"category": "grid", "counterparty": "binance_spot"},
    "grid_sol": {"category": "grid", "counterparty": "binance_spot"},
    "grid_stables": {"category": "grid_stables", "counterparty": "binance_spot"},
    "tv_momentum": {"category": "momentum", "counterparty": "binance_spot"},
    "xstocks_directional": {"category": "tokenized_stock", "counterparty": "backed_xstocks"},
    "xstocks_grid": {"category": "tokenized_stock", "counterparty": "backed_xstocks"},
    "crypto_memecoins": {"category": "memecoin", "counterparty": "coingecko_cex"},
    "wow_sniper_base": {"category": "sniper", "counterparty": "base_dex"},
}


# ---------- policy / data loaders ----------


def _load_policy() -> dict:
    if not POLICY_FILE.exists():
        return {}
    try:
        return json.loads(POLICY_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _load_live_portfolio() -> list:
    if not LIVE_PORTFOLIO.exists():
        return []
    try:
        d = json.loads(LIVE_PORTFOLIO.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return d.get("positions", []) if isinstance(d, dict) else (d or [])


def _load_summary() -> dict:
    if not PORTFOLIO_SUMMARY.exists():
        return {"sleeves": {}}
    try:
        return json.loads(PORTFOLIO_SUMMARY.read_text())
    except (OSError, json.JSONDecodeError):
        return {"sleeves": {}}


# ---------- realized vol ----------


def realized_vol_pct(sleeve_id: str, worker_name: str, positions: list) -> float:
    """
    Annualized realized vol for the worker's contribution to this sleeve.

    Uses standard deviation of per-position PnL-over-principal, scaled to
    annual assuming ~365 positions per year. With fewer than 5 resolved
    positions, falls back to the bootstrap number for the worker's category.
    """
    pnl_ratios = []
    for p in positions:
        if not isinstance(p, dict):
            continue
        if p.get("worker") != worker_name:
            continue
        if p.get("sleeve") != sleeve_id:
            continue
        if not p.get("resolved"):
            continue
        principal = p.get("principal_usd") or p.get("size_usd") or 0
        pnl = p.get("pnl_usd", 0) or 0
        if principal <= 0:
            continue
        pnl_ratios.append(pnl / principal)

    if len(pnl_ratios) < 5:
        cat = _WORKER_META.get(worker_name, {}).get("category", "default")
        return _BOOTSTRAP_VOL_PCT.get(cat, _BOOTSTRAP_VOL_PCT["default"])

    per_trade_std = statistics.pstdev(pnl_ratios)
    # Annualize assuming ~365 trades/yr upper bound; scale by sqrt(N/yr).
    # Conservative: if we have N resolved across the data horizon, estimate
    # vol per trade and annualize with sqrt(365 / horizon_days).
    # Without horizon data, use sqrt(365) as a generic annualizer.
    annualized = per_trade_std * math.sqrt(365) * 100  # as %
    return max(0.01, annualized)


# ---------- drawdown ----------


def fund_drawdown_pct(fund_id: str, summary: dict) -> float:
    """Current drawdown = cumulative fund PnL / fund capital (%)."""
    capital = _load_policy().get("funds", {}).get(fund_id, {}).get("capital_usd", 1000.0)
    pnl = 0.0
    for sid, s in summary.get("sleeves", {}).items():
        if sid.startswith(fund_id + "."):
            pnl += float(s.get("pnl_usd") or 0)
    if capital <= 0:
        return 0.0
    return (pnl / capital) * 100.0


# ---------- concentration ----------


def counterparty_exposure_pct(fund_id: str, policy: dict) -> dict:
    """Return {counterparty: % of fund capital} for every counterparty in this fund."""
    fund = policy.get("funds", {}).get(fund_id, {})
    capital = float(fund.get("capital_usd", 1000.0))
    out: dict[str, float] = defaultdict(float)
    if capital <= 0:
        return dict(out)
    for sleeve in fund.get("sleeves", {}).values():
        for wname, wcfg in sleeve.get("workers", {}).items():
            principal = wcfg.get("principal_usd") or wcfg.get("target_deployment_usd") or 0
            cp = _WORKER_META.get(wname, {}).get("counterparty", wname)
            out[cp] += float(principal) / capital * 100.0
    return dict(out)


# ---------- core sizing ----------


def _sized_for_sleeve(
    fund_id: str,
    sleeve_id_short: str,
    worker_name: str,
    static_usd: float,
    policy: dict,
    positions: list,
    summary: dict,
) -> tuple[float, dict]:
    """
    Return (sized_usd, attribution_dict) for a single sleeve.

    attribution explains the size: static, vol_cap, kelly, concentration,
    drawdown_halted — so --show can render the math.
    """
    risk = policy.get("risk", {})
    fund = policy.get("funds", {}).get(fund_id, {})
    capital = float(fund.get("capital_usd", 1000.0))
    n_sleeves = max(1, len(fund.get("sleeves", {})))
    target_vol_pct = float(risk.get("target_portfolio_vol_pct", 8.0))
    kelly = float(risk.get("kelly_fraction", 0.25))
    max_cp_pct = float(risk.get("max_concentration_per_counterparty_pct", 30.0))
    max_dd_halt = risk.get("max_drawdown_halt_per_fund_pct")  # may be null

    sleeve_id = f"{fund_id}.{sleeve_id_short}"
    vol_pct = realized_vol_pct(sleeve_id, worker_name, positions)

    # Per-sleeve vol budget assumes uncorrelated sleeves — sqrt(n) divisor.
    per_sleeve_vol = target_vol_pct / math.sqrt(n_sleeves)
    vol_cap_usd = capital * per_sleeve_vol / vol_pct if vol_pct > 0 else float("inf")

    base = min(static_usd, vol_cap_usd)
    after_kelly = base * kelly

    dd_pct = fund_drawdown_pct(fund_id, summary)
    halted = bool(max_dd_halt is not None and dd_pct <= -float(max_dd_halt))
    if halted:
        sized = 0.0
    else:
        sized = after_kelly

    # Counterparty concentration: pro-rata scale if this worker's counterparty
    # already exceeds max_cp_pct of fund capital at the base (pre-engine)
    # deployment plan.
    cp = _WORKER_META.get(worker_name, {}).get("counterparty", worker_name)
    cp_pcts = counterparty_exposure_pct(fund_id, policy)
    cp_current_pct = cp_pcts.get(cp, 0.0)
    cp_scalar = 1.0
    if cp_current_pct > max_cp_pct and cp_current_pct > 0:
        cp_scalar = max_cp_pct / cp_current_pct
        sized *= cp_scalar

    return sized, {
        "static_usd": round(static_usd, 4),
        "realized_vol_pct": round(vol_pct, 3),
        "vol_cap_usd": round(vol_cap_usd, 2) if math.isfinite(vol_cap_usd) else None,
        "kelly_fraction": kelly,
        "after_kelly_usd": round(after_kelly, 4),
        "fund_drawdown_pct": round(dd_pct, 3),
        "halted_on_drawdown": halted,
        "counterparty": cp,
        "counterparty_exposure_pct": round(cp_current_pct, 2),
        "counterparty_scalar": round(cp_scalar, 4),
        "sized_usd": round(sized, 4),
    }


def apply_engine(worker_name: str, static_targets: dict) -> dict:
    """
    Main entry point called from policy.sleeve_targets_for() when the engine
    is enabled. Takes the static {sleeve_id: usd} dict and returns the
    engine-adjusted version.

    Static values pass through if engine_enabled=false.
    """
    policy = _load_policy()
    risk = policy.get("risk", {})
    if not risk.get("engine_enabled"):
        return static_targets

    positions = _load_live_portfolio()
    summary = _load_summary()

    out = {}
    for sleeve_key, static_usd in static_targets.items():
        if "." in sleeve_key:
            fund_id, sleeve_short = sleeve_key.split(".", 1)
        else:
            fund_id, sleeve_short = "", sleeve_key
        sized, _ = _sized_for_sleeve(
            fund_id,
            sleeve_short,
            worker_name,
            float(static_usd),
            policy,
            positions,
            summary,
        )
        out[sleeve_key] = round(sized, 4)
    return out


# ---------- CLI ----------


def _print_table(worker_names: list[str], engine_on: bool):
    policy = _load_policy()
    positions = _load_live_portfolio()
    summary = _load_summary()

    print(f"risk engine: enabled={engine_on}")
    risk = policy.get("risk", {})
    print(f"  kelly_fraction                         = {risk.get('kelly_fraction')}")
    print(f"  target_portfolio_vol_pct               = {risk.get('target_portfolio_vol_pct')}")
    print(
        f"  max_concentration_per_counterparty_pct = {risk.get('max_concentration_per_counterparty_pct')}"
    )
    print(
        f"  max_drawdown_halt_per_fund_pct         = {risk.get('max_drawdown_halt_per_fund_pct')}"
    )
    print()

    # fund-level summary
    for fund_id in policy.get("funds", {}):
        dd = fund_drawdown_pct(fund_id, summary)
        exposures = counterparty_exposure_pct(fund_id, policy)
        print(
            f"== {fund_id}  capital=${policy['funds'][fund_id].get('capital_usd', 0):.0f}  drawdown={dd:+.2f}%"
        )
        print(
            "   counterparty exposure: "
            + ", ".join(
                f"{cp}={pct:.1f}%" for cp, pct in sorted(exposures.items(), key=lambda x: -x[1])
            )
        )

    print()
    hdr = (
        "worker",
        "fund",
        "sleeve",
        "static",
        "vol%",
        "vol_cap",
        "kelly",
        "dd_halt",
        "cp×",
        "sized",
    )
    print(
        f"{hdr[0]:<22} {hdr[1]:<22} {hdr[2]:<22} {hdr[3]:>8} {hdr[4]:>6} {hdr[5]:>10} {hdr[6]:>6} {hdr[7]:>7} {hdr[8]:>6} {hdr[9]:>10}"
    )
    print("-" * 130)
    for wn in worker_names:
        # Read the raw policy values directly — do NOT go through
        # policy.sleeve_targets_for, which would re-enter the engine.
        static = _static_sleeve_targets_direct(policy, wn)
        if not static:
            continue
        for sk, static_usd in static.items():
            fund_id, sleeve_short = sk.split(".", 1) if "." in sk else ("", sk)
            sized, attr = _sized_for_sleeve(
                fund_id,
                sleeve_short,
                wn,
                float(static_usd),
                policy,
                positions,
                summary,
            )
            # If engine disabled, sized prints as the static value
            display_sized = sized if engine_on else float(static_usd)
            vol_cap_s = (
                f"${attr['vol_cap_usd']:,.0f}" if attr["vol_cap_usd"] is not None else "     inf"
            )
            dd_s = "YES" if attr["halted_on_drawdown"] else "no"
            print(
                f"{wn:<22} {fund_id:<22} {sleeve_short:<22} ${static_usd:>7.2f} {attr['realized_vol_pct']:>5.1f}% {vol_cap_s:>10} {attr['kelly_fraction']:>6.2f} {dd_s:>7} {attr['counterparty_scalar']:>5.2f}× ${display_sized:>9.2f}"
            )
    print()


def _static_sleeve_targets_direct(policy: dict, worker_name: str) -> dict:
    """Same logic as policy.sleeve_targets_for but reads a pre-loaded policy dict."""
    out = {}
    for fund_id, fund in policy.get("funds", {}).items():
        for sleeve_id, sleeve in fund.get("sleeves", {}).items():
            entry = sleeve.get("workers", {}).get(worker_name)
            if not entry:
                continue
            val = (
                entry.get("principal_usd")
                or entry.get("target_deployment_usd")
                or entry.get("principal_usd_per_symbol")
            )
            if val is None:
                continue
            out[f"{fund_id}.{sleeve_id}"] = float(val)
    return out


def _flip_engine(enabled: bool):
    p = _load_policy()
    p.setdefault("risk", {})["engine_enabled"] = enabled
    POLICY_FILE.write_text(json.dumps(p, indent=2))
    print(f"policy.risk.engine_enabled = {enabled}  (written to {POLICY_FILE})")


def main():
    ap = argparse.ArgumentParser(description="Hermes risk engine CLI")
    ap.add_argument("--show", action="store_true", help="Print per-worker sizing table")
    ap.add_argument("--enable", action="store_true", help="Flip engine_enabled=true in policy.json")
    ap.add_argument(
        "--disable", action="store_true", help="Flip engine_enabled=false in policy.json"
    )
    ap.add_argument(
        "--enable-preview",
        action="store_true",
        help="Show sizing as if the engine were enabled (without flipping the flag)",
    )
    ap.add_argument(
        "--worker", action="append", default=None, help="Restrict --show to specific worker(s)"
    )
    args = ap.parse_args()

    if args.enable:
        _flip_engine(True)
        return
    if args.disable:
        _flip_engine(False)
        return

    if args.show or args.enable_preview:
        workers = args.worker or list(_WORKER_META.keys())
        engine_on = (
            True
            if args.enable_preview
            else _load_policy().get("risk", {}).get("engine_enabled", False)
        )
        _print_table(workers, engine_on)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
