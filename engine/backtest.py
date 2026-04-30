#!/usr/bin/env python3
"""
Backtest harness — synthetic Monte Carlo over the current policy + fund set.

Simulates N paths of `days` trading days per fund. Each day:
  1. The risk engine (if enabled) sizes each sleeve from the state so far
     (cumulative fund PnL for drawdown halt, counterparty exposures for
     concentration cap).
  2. A daily return is drawn per sleeve from Normal(mu/252, sigma/sqrt(252))
     where mu, sigma come from the per-strategy STRATEGY_STATS table below.
  3. Sleeve PnL = size_usd × daily_return is added to the fund book.

Outputs per-fund distributions of Sharpe, Sortino, max-drawdown, CAGR,
daily-win-rate, annualized vol. Results are printed as a table and
optionally dumped as JSON + CSV equity curves.

This is synthetic data — it validates the *fund structure* and *engine
behavior* under known vol/mu assumptions. Validating the vol *values*
themselves needs real price history (separate PR).

CLI:
  python3 funds/backtest.py                       # 252 days × 100 sims, engine-off
  python3 funds/backtest.py --sims 500 --days 365
  python3 funds/backtest.py --compare             # engine on vs engine off
  python3 funds/backtest.py --kelly-sweep         # sweep kelly_fraction 0.1-1.0
  python3 funds/backtest.py --fund fund_60_40_income
  python3 funds/backtest.py --output-dir exports/bt_2026-04-23
  python3 funds/backtest.py --seed 42
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from engine import policy, risk_engine

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_OUTPUT = REPO_ROOT / "exports"
TRADING_DAYS_PER_YEAR = 252


# Per-strategy category expected annual return + annual vol. Vol matches
# risk_engine._BOOTSTRAP_VOL_PCT for consistency. Mu is a pragmatic default
# calibrated to the strategy's stated thesis.
STRATEGY_STATS = {
    "yield": {"mu": 4.0, "sigma": 1.0},
    "delta_neutral": {"mu": 8.0, "sigma": 3.0},
    "grid": {"mu": 12.0, "sigma": 8.0},
    "grid_stables": {"mu": 3.0, "sigma": 0.5},
    "directional": {"mu": 15.0, "sigma": 25.0},
    "momentum": {"mu": 15.0, "sigma": 25.0},
    "binary": {"mu": 5.0, "sigma": 40.0},
    "tokenized_stock": {"mu": 10.0, "sigma": 20.0},
    "memecoin": {"mu": 30.0, "sigma": 80.0},
    "sniper": {"mu": 50.0, "sigma": 120.0},
    "default": {"mu": 10.0, "sigma": 15.0},
}


# ---------- helpers ----------


def _category(worker_name: str) -> str:
    return risk_engine._WORKER_META.get(worker_name, {}).get("category", "default")


def _counterparty(worker_name: str) -> str:
    return risk_engine._WORKER_META.get(worker_name, {}).get("counterparty", worker_name)


def _daily_params(mu_annual_pct: float, sigma_annual_pct: float) -> tuple[float, float]:
    """Convert annualized % to daily linear-return parameters."""
    mu = (mu_annual_pct / 100.0) / TRADING_DAYS_PER_YEAR
    sigma = (sigma_annual_pct / 100.0) / math.sqrt(TRADING_DAYS_PER_YEAR)
    return mu, sigma


@dataclass
class SleeveLeg:
    fund_id: str
    sleeve_short: str
    worker: str
    static_usd: float
    category: str
    counterparty: str


def _collect_legs(policy_snapshot: dict, fund_filter: str | None = None) -> list[SleeveLeg]:
    out = []
    for fund_id, fund in policy_snapshot.get("funds", {}).items():
        if fund_filter and fund_id != fund_filter:
            continue
        for sleeve_short, sleeve in fund.get("sleeves", {}).items():
            for wname, wcfg in sleeve.get("workers", {}).items():
                principal = (
                    wcfg.get("principal_usd")
                    or wcfg.get("target_deployment_usd")
                    or wcfg.get("principal_usd_per_symbol")
                    or 0.0
                )
                if principal <= 0:
                    continue
                out.append(
                    SleeveLeg(
                        fund_id=fund_id,
                        sleeve_short=sleeve_short,
                        worker=wname,
                        static_usd=float(principal),
                        category=_category(wname),
                        counterparty=_counterparty(wname),
                    )
                )
    return out


# ---------- metrics ----------


def _max_drawdown_pct(equity: list[float]) -> float:
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (v - peak) / peak * 100
            if dd < max_dd:
                max_dd = dd
    return max_dd


def _sharpe(daily_returns: list[float]) -> float:
    if len(daily_returns) < 2:
        return 0.0
    mean = statistics.mean(daily_returns)
    std = statistics.pstdev(daily_returns)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(TRADING_DAYS_PER_YEAR)


def _sortino(daily_returns: list[float]) -> float:
    if len(daily_returns) < 2:
        return 0.0
    mean = statistics.mean(daily_returns)
    downside = [r for r in daily_returns if r < 0]
    if len(downside) < 2:
        return 0.0
    dstd = statistics.pstdev(downside)
    if dstd == 0:
        return 0.0
    return (mean / dstd) * math.sqrt(TRADING_DAYS_PER_YEAR)


def _cagr_pct(equity: list[float], days: int) -> float:
    if not equity or equity[0] <= 0:
        return 0.0
    years = days / TRADING_DAYS_PER_YEAR
    if years <= 0:
        return 0.0
    ratio = equity[-1] / equity[0]
    if ratio <= 0:
        return -100.0
    return (ratio ** (1 / years) - 1) * 100


def _ann_vol_pct(daily_returns: list[float]) -> float:
    if len(daily_returns) < 2:
        return 0.0
    return statistics.pstdev(daily_returns) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100


def _win_rate_pct(daily_returns: list[float]) -> float:
    if not daily_returns:
        return 0.0
    wins = sum(1 for r in daily_returns if r > 0)
    return wins / len(daily_returns) * 100


def _compute_metrics(equity: list[float], daily_returns: list[float], days: int) -> dict:
    return {
        "sharpe": round(_sharpe(daily_returns), 3),
        "sortino": round(_sortino(daily_returns), 3),
        "max_dd_pct": round(_max_drawdown_pct(equity), 3),
        "cagr_pct": round(_cagr_pct(equity, days), 3),
        "ann_vol_pct": round(_ann_vol_pct(daily_returns), 3),
        "win_rate_pct": round(_win_rate_pct(daily_returns), 3),
        "final_equity": round(equity[-1], 2) if equity else 0.0,
    }


# ---------- engine-aware sizing per day ----------


def _counterparty_exposure_from_sizes(fund_id: str, sizes: dict, capital: float) -> dict:
    """sizes: {(fund_id, sleeve_short, worker): usd}. Return {cp: pct_of_capital}."""
    out: defaultdict[str, float] = defaultdict(float)
    if capital <= 0:
        return dict(out)
    for (fid, _sleeve, worker), usd in sizes.items():
        if fid != fund_id:
            continue
        cp = _counterparty(worker)
        out[cp] += usd / capital * 100.0
    return dict(out)


def _size_for_day(
    leg: SleeveLeg, cum_fund_pnl: float, policy_snapshot: dict, cp_exposure: dict
) -> float:
    """Return engine-adjusted USD for one sleeve-leg on a given day."""
    risk = policy_snapshot.get("risk", {})
    if not risk.get("engine_enabled"):
        return leg.static_usd

    fund = policy_snapshot.get("funds", {}).get(leg.fund_id, {})
    capital = float(fund.get("capital_usd", 1000.0))
    n_sleeves = max(1, len(fund.get("sleeves", {})))
    target_vol = float(risk.get("target_portfolio_vol_pct", 8.0))
    kelly = float(risk.get("kelly_fraction", 0.25))
    max_cp_pct = float(risk.get("max_concentration_per_counterparty_pct", 30.0))
    dd_halt = risk.get("max_drawdown_halt_per_fund_pct")

    sigma = STRATEGY_STATS.get(leg.category, STRATEGY_STATS["default"])["sigma"]
    per_sleeve_vol = target_vol / math.sqrt(n_sleeves)
    vol_cap = capital * per_sleeve_vol / sigma if sigma > 0 else float("inf")

    base = min(leg.static_usd, vol_cap)
    after_kelly = base * kelly

    if dd_halt is not None and capital > 0:
        dd_pct = (cum_fund_pnl / capital) * 100
        if dd_pct <= -float(dd_halt):
            return 0.0

    cp = leg.counterparty
    cp_current_pct = cp_exposure.get(cp, 0.0)
    if cp_current_pct > max_cp_pct and cp_current_pct > 0:
        after_kelly *= max_cp_pct / cp_current_pct

    return after_kelly


# ---------- single MC path ----------


def _simulate_one(
    policy_snapshot: dict, legs: list[SleeveLeg], days: int, rng: random.Random
) -> dict:
    """Run one Monte Carlo path across all funds. Return {fund_id: metrics}."""
    # Pre-compute the static counterparty exposure (used when engine off).
    static_sizes = {(leg.fund_id, leg.sleeve_short, leg.worker): leg.static_usd for leg in legs}

    fund_capitals = {
        fid: float(f.get("capital_usd", 1000.0))
        for fid, f in policy_snapshot.get("funds", {}).items()
    }

    fund_equity: dict[str, list[float]] = {fid: [cap] for fid, cap in fund_capitals.items()}
    fund_daily_rets: dict[str, list[float]] = {fid: [] for fid in fund_capitals}
    fund_cum_pnl = {fid: 0.0 for fid in fund_capitals}

    # Pre-draw daily returns per leg — independent samples (no correlation).
    daily_draws = []
    for _ in range(days):
        today = {}
        for leg in legs:
            stats = STRATEGY_STATS.get(leg.category, STRATEGY_STATS["default"])
            mu, sigma = _daily_params(stats["mu"], stats["sigma"])
            today[(leg.fund_id, leg.sleeve_short, leg.worker)] = rng.gauss(mu, sigma)
        daily_draws.append(today)

    for day_idx in range(days):
        draws = daily_draws[day_idx]
        # Today's sizing per leg (engine evaluates per-day using running state).
        today_sizes = {}
        cp_exposures_per_fund = {
            fid: _counterparty_exposure_from_sizes(fid, static_sizes, cap)
            for fid, cap in fund_capitals.items()
        }
        for leg in legs:
            sz = _size_for_day(
                leg,
                fund_cum_pnl[leg.fund_id],
                policy_snapshot,
                cp_exposures_per_fund[leg.fund_id],
            )
            today_sizes[(leg.fund_id, leg.sleeve_short, leg.worker)] = sz

        day_pnl_by_fund: defaultdict[str, float] = defaultdict(float)
        for leg in legs:
            key = (leg.fund_id, leg.sleeve_short, leg.worker)
            sz = today_sizes[key]
            r = draws[key]
            day_pnl_by_fund[leg.fund_id] += sz * r

        for fid in fund_capitals:
            dpnl = day_pnl_by_fund[fid]
            fund_cum_pnl[fid] += dpnl
            eq = fund_capitals[fid] + fund_cum_pnl[fid]
            fund_equity[fid].append(eq)
            # daily return on fund equity (previous-day denominator)
            prev = fund_equity[fid][-2]
            if prev > 0:
                fund_daily_rets[fid].append(dpnl / prev)
            else:
                fund_daily_rets[fid].append(0.0)

    out = {}
    for fid in fund_capitals:
        out[fid] = _compute_metrics(fund_equity[fid], fund_daily_rets[fid], days)
        out[fid]["equity_curve"] = fund_equity[fid]
    return out


def _pctl(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def _aggregate(per_sim: list[dict], metric_keys: list[str]) -> dict:
    """Aggregate a list of per-sim per-fund metrics into p5/p50/p95 + mean."""
    all_fids: set[str] = set()
    for r in per_sim:
        all_fids.update(r.keys())
    agg: dict[str, dict] = {}
    for fid in sorted(all_fids):
        agg[fid] = {}
        for key in metric_keys:
            vals = [r[fid][key] for r in per_sim if fid in r and key in r[fid]]
            if not vals:
                continue
            agg[fid][key] = {
                "p5": round(_pctl(vals, 5), 3),
                "p50": round(_pctl(vals, 50), 3),
                "p95": round(_pctl(vals, 95), 3),
                "mean": round(statistics.mean(vals), 3),
            }
    return agg


def run_mc(
    policy_snapshot: dict, days: int, sims: int, seed: int, fund_filter: str | None = None
) -> dict:
    legs = _collect_legs(policy_snapshot, fund_filter=fund_filter)
    if not legs:
        return {"fund_filter": fund_filter, "error": "no legs found"}

    rng = random.Random(seed)
    per_sim = []
    equity_samples = defaultdict(list)  # fund_id -> [curve for sim 0 ... min(5, sims)]
    for i in range(sims):
        sub_rng = random.Random(rng.random())
        result = _simulate_one(policy_snapshot, legs, days, sub_rng)
        sim_metrics = {}
        for fid, m in result.items():
            sim_metrics[fid] = {k: v for k, v in m.items() if k != "equity_curve"}
            if i < 5:
                equity_samples[fid].append(m["equity_curve"])
        per_sim.append(sim_metrics)

    metric_keys = [
        "sharpe",
        "sortino",
        "max_dd_pct",
        "cagr_pct",
        "ann_vol_pct",
        "win_rate_pct",
        "final_equity",
    ]
    agg = _aggregate(per_sim, metric_keys)

    return {
        "sims": sims,
        "days": days,
        "seed": seed,
        "fund_filter": fund_filter,
        "engine_enabled": bool(policy_snapshot.get("risk", {}).get("engine_enabled")),
        "kelly_fraction": policy_snapshot.get("risk", {}).get("kelly_fraction"),
        "aggregated": agg,
        "equity_samples": {fid: curves for fid, curves in equity_samples.items()},
    }


# ---------- CLI output ----------


def _print_summary(result: dict, label: str = ""):
    if "error" in result:
        print(f"[backtest] {result['error']}")
        return
    print()
    hdr = (
        f"=== Backtest {label} | sims={result['sims']} days={result['days']} "
        f"engine={'ON' if result['engine_enabled'] else 'off'} "
        f"kelly={result['kelly_fraction']} seed={result['seed']} ==="
    )
    print(hdr)
    keys = ["sharpe", "sortino", "max_dd_pct", "cagr_pct", "ann_vol_pct", "win_rate_pct"]
    labels = {
        "sharpe": "Sharpe",
        "sortino": "Sortino",
        "max_dd_pct": "MaxDD%",
        "cagr_pct": "CAGR%",
        "ann_vol_pct": "AnnVol%",
        "win_rate_pct": "WinRate%",
    }
    for fid, metrics in result["aggregated"].items():
        print(f"\n{fid}")
        print(f"  {'percentile':<6}  " + "  ".join(f"{labels[k]:>10}" for k in keys))
        for p in ("p5", "p50", "p95"):
            row = "  ".join(
                f"{metrics[k][p]:>10.3f}" if k in metrics else "       n/a" for k in keys
            )
            print(f"  {p:<6}  {row}")


def _print_compare(off_result: dict, on_result: dict):
    print()
    print("=== Engine ON vs OFF (p50 delta) ===")
    keys = ["sharpe", "sortino", "max_dd_pct", "cagr_pct", "ann_vol_pct"]
    labels = {
        "sharpe": "ΔSharpe",
        "sortino": "ΔSortino",
        "max_dd_pct": "ΔMaxDD%",
        "cagr_pct": "ΔCAGR%",
        "ann_vol_pct": "ΔAnnVol%",
    }
    fids = sorted(set(off_result["aggregated"].keys()) | set(on_result["aggregated"].keys()))
    header = "  ".join(f"{labels[k]:>10}" for k in keys)
    print(f"{'fund':<22}  {header}")
    for fid in fids:
        off_m = off_result["aggregated"].get(fid, {})
        on_m = on_result["aggregated"].get(fid, {})
        row = []
        for k in keys:
            if k in off_m and k in on_m:
                d = on_m[k]["p50"] - off_m[k]["p50"]
                row.append(f"{d:>+10.3f}")
            else:
                row.append(f"{'n/a':>10}")
        print(f"{fid:<22}  " + "  ".join(row))


def _write_outputs(result: dict, out_dir: Path, tag: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"backtest_{tag}.json").write_text(json.dumps(result, indent=2))
    # Equity curves as CSV — one column per sim sample
    import csv

    for fid, curves in result.get("equity_samples", {}).items():
        if not curves:
            continue
        path = out_dir / f"equity_{fid}_{tag}.csv"
        ncols = len(curves)
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["day"] + [f"sim_{i}" for i in range(ncols)])
            max_len = max(len(c) for c in curves)
            for d in range(max_len):
                row: list = [d]
                for c in curves:
                    row.append(c[d] if d < len(c) else "")
                w.writerow(row)


# ---------- Kelly sweep ----------


def _run_kelly_sweep(
    policy_snapshot: dict,
    days: int,
    sims: int,
    seed: int,
    fund_filter: str | None,
    fractions: list[float],
) -> list[dict]:
    rows = []
    for k in fractions:
        snap = copy.deepcopy(policy_snapshot)
        snap.setdefault("risk", {})["engine_enabled"] = True
        snap["risk"]["kelly_fraction"] = k
        res = run_mc(snap, days, sims, seed, fund_filter=fund_filter)
        rows.append({"kelly_fraction": k, "result": res})
    return rows


def _print_kelly_sweep(rows: list[dict]):
    print()
    print("=== Kelly sweep (engine ON, varying kelly_fraction) ===")
    fids = sorted({fid for r in rows for fid in r["result"]["aggregated"]})
    for fid in fids:
        print(f"\n{fid}")
        print(f"  {'kelly':>6}  {'Sharpe':>8}  {'MaxDD%':>8}  {'CAGR%':>8}  {'AnnVol%':>8}")
        for r in rows:
            m = r["result"]["aggregated"].get(fid, {})
            if not m:
                continue
            print(
                f"  {r['kelly_fraction']:>6.2f}  "
                f"{m.get('sharpe', {}).get('p50', 0):>8.3f}  "
                f"{m.get('max_dd_pct', {}).get('p50', 0):>8.3f}  "
                f"{m.get('cagr_pct', {}).get('p50', 0):>8.3f}  "
                f"{m.get('ann_vol_pct', {}).get('p50', 0):>8.3f}"
            )


# ---------- main ----------


def main():
    ap = argparse.ArgumentParser(description="Hermes synthetic MC backtest")
    ap.add_argument(
        "--days", type=int, default=252, help="Trading days per sim (default 252 = 1yr)"
    )
    ap.add_argument(
        "--sims", type=int, default=100, help="Monte Carlo paths per fund (default 100)"
    )
    ap.add_argument("--seed", type=int, default=42, help="RNG seed")
    ap.add_argument("--fund", type=str, default=None, help="Restrict to one fund_id")
    ap.add_argument("--compare", action="store_true", help="Run engine OFF and ON, print the delta")
    ap.add_argument(
        "--kelly-sweep", action="store_true", help="Sweep kelly_fraction 0.10..1.00 (engine ON)"
    )
    ap.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Write JSON + equity CSVs to this dir (default: skip)",
    )
    args = ap.parse_args()

    policy._load_policy.cache_clear()
    snap = policy._load_policy()

    tag = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    if args.kelly_sweep:
        rows = _run_kelly_sweep(
            snap, args.days, args.sims, args.seed, args.fund, [0.10, 0.25, 0.50, 0.75, 1.00]
        )
        _print_kelly_sweep(rows)
        if args.output_dir:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            (args.output_dir / f"kelly_sweep_{tag}.json").write_text(json.dumps(rows, indent=2))
        return

    if args.compare:
        off_snap = copy.deepcopy(snap)
        off_snap.setdefault("risk", {})["engine_enabled"] = False
        on_snap = copy.deepcopy(snap)
        on_snap.setdefault("risk", {})["engine_enabled"] = True
        off_result = run_mc(off_snap, args.days, args.sims, args.seed, args.fund)
        on_result = run_mc(on_snap, args.days, args.sims, args.seed, args.fund)
        _print_summary(off_result, label="(engine OFF)")
        _print_summary(on_result, label="(engine ON)")
        _print_compare(off_result, on_result)
        if args.output_dir:
            _write_outputs(off_result, args.output_dir, f"off_{tag}")
            _write_outputs(on_result, args.output_dir, f"on_{tag}")
        return

    result = run_mc(snap, args.days, args.sims, args.seed, args.fund)
    _print_summary(result)
    if args.output_dir:
        _write_outputs(result, args.output_dir, tag)


if __name__ == "__main__":
    main()
