#!/usr/bin/env python3
"""
NAV accounting — unit pricing, management + performance fee accrual, HWM tracking.

Every fund holds:
  - units_outstanding    (share count; starts at capital / $1.0000)
  - hwm_per_unit         (high-water mark — perf fee paid only above this)
  - cumulative mgmt + perf fees paid to date

NAV per unit (net of accrued-but-unpaid fees) is the number an investor would
receive on redemption today. The accrual model is continuous daily:
  - mgmt fee accrues = gross_fund_value * mgmt_rate / 365 per day
  - perf fee accrues = 20% × (NAV - HWM) × units   if NAV > HWM  else 0
Crystallization (accrued -> paid) happens on the cadence configured in policy
(monthly for mgmt, quarterly/annual for perf). `crystallize(fund_id)` moves
the accrued amount into cumulative_paid and reduces units_outstanding by
fee_usd / nav_per_unit (fee is paid in units back to the manager).

Inputs:
  config/policy.json                (fund fees, capital, sleeve targets)
  data/portfolio_summary.json       (current gross PnL per fund)
  data/nav_ledger.json              (persistent: units, hwm, cum fees)

Outputs:
  data/nav_ledger.json              (updated on crystallize)
  statement dicts from generate_statement(fund_id, period=YYYY-MM)

CLI:
  python3 funds/nav_accounting.py --show                   current NAV per fund
  python3 funds/nav_accounting.py --show --fund fund_X
  python3 funds/nav_accounting.py --statement 2026-04      monthly statement
  python3 funds/nav_accounting.py --statement 2026-Q2 --fund fund_60_40_income
  python3 funds/nav_accounting.py --crystallize-mgmt       accrue+pay mgmt fees
  python3 funds/nav_accounting.py --crystallize-perf       accrue+pay perf fees
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_FILE = REPO_ROOT / "config" / "policy.json"
PORTFOLIO_SUMMARY = REPO_ROOT / "data" / "portfolio_summary.json"
NAV_LEDGER = REPO_ROOT / "data" / "nav_ledger.json"


# ---------- IO ----------


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _save_json_atomic(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _now() -> datetime:
    return datetime.now(UTC)


# ---------- core NAV ----------


@dataclass
class NavSnapshot:
    fund_id: str
    as_of: str
    capital_usd: float
    gross_pnl_usd: float
    gross_equity_usd: float
    units_outstanding: float
    nav_per_unit_gross: float
    accrued_mgmt_fee_usd: float
    accrued_perf_fee_usd: float
    nav_per_unit_net: float
    hwm_per_unit: float
    cumulative_mgmt_fees_paid_usd: float
    cumulative_perf_fees_paid_usd: float
    return_since_inception_pct: float
    days_since_inception: int
    annualized_return_pct: float


def _fund_cumulative_pnl(summary: dict, fund_id: str) -> float:
    total = 0.0
    for sid, s in summary.get("sleeves", {}).items():
        if sid.startswith(fund_id + "."):
            total += float(s.get("pnl_usd") or 0)
    return total


def _accrued_mgmt_fee(
    gross_equity: float, mgmt_rate_annual: float, days_since_crystallization: int
) -> float:
    """Daily linear accrual on gross equity since last crystallization."""
    if mgmt_rate_annual <= 0 or days_since_crystallization <= 0:
        return 0.0
    return gross_equity * (mgmt_rate_annual / 100.0) * (days_since_crystallization / 365.0)


def _accrued_perf_fee(
    nav_per_unit: float,
    hwm_per_unit: float,
    units: float,
    perf_rate: float,
    hurdle_rate_annual: float,
    days_since_crystallization: int,
) -> float:
    """Perf fee on the portion above HWM (with optional hurdle)."""
    if perf_rate <= 0 or units <= 0:
        return 0.0
    # Effective high-water line = max(HWM, HWM + hurdle accrual) — hurdle
    # protects up to hurdle_rate; perf fee only applies above that.
    hurdle_factor = 1.0 + (hurdle_rate_annual / 100.0) * (days_since_crystallization / 365.0)
    effective_hwm = hwm_per_unit * hurdle_factor
    if nav_per_unit <= effective_hwm:
        return 0.0
    per_unit_gain = nav_per_unit - effective_hwm
    return per_unit_gain * units * (perf_rate / 100.0)


def compute_nav(
    fund_id: str,
    policy: dict | None = None,
    summary: dict | None = None,
    ledger: dict | None = None,
) -> NavSnapshot | None:
    policy = policy if policy is not None else _load_json(POLICY_FILE, {})
    summary = summary if summary is not None else _load_json(PORTFOLIO_SUMMARY, {"sleeves": {}})
    ledger = ledger if ledger is not None else _load_json(NAV_LEDGER, {"funds": {}})

    fund_cfg = policy.get("funds", {}).get(fund_id)
    ledger_entry = ledger.get("funds", {}).get(fund_id)
    if not fund_cfg or not ledger_entry:
        return None

    fees = fund_cfg.get("fees", {}) or {}
    mgmt_rate = float(fees.get("management_fee_annual_pct", 0.0) or 0.0)
    perf_rate = float(fees.get("performance_fee_pct", 0.0) or 0.0)
    hurdle_rate = float(fees.get("hurdle_rate_annual_pct", 0.0) or 0.0)

    capital = float(fund_cfg.get("capital_usd", 0.0))
    gross_pnl = _fund_cumulative_pnl(summary, fund_id)
    gross_equity = (
        capital
        + gross_pnl
        - float(ledger_entry.get("cumulative_mgmt_fees_paid_usd", 0.0))
        - float(ledger_entry.get("cumulative_perf_fees_paid_usd", 0.0))
    )

    units = float(ledger_entry.get("units_outstanding", capital))
    nav_per_unit_gross = (gross_equity / units) if units > 0 else 0.0
    hwm = float(ledger_entry.get("hwm_per_unit", 1.0))

    now = _now()
    last_mgmt = _parse_iso(
        ledger_entry.get("last_mgmt_crystallization_date", ledger_entry.get("inception_date"))
    )
    last_perf = _parse_iso(
        ledger_entry.get("last_perf_crystallization_date", ledger_entry.get("inception_date"))
    )
    inception = _parse_iso(ledger_entry.get("inception_date"))

    days_since_mgmt = max(0, (now - last_mgmt).days)
    days_since_perf = max(0, (now - last_perf).days)
    days_since_inception = max(0, (now - inception).days)

    mgmt_accrued = _accrued_mgmt_fee(gross_equity, mgmt_rate, days_since_mgmt)
    # Perf fee is computed on NAV *net of mgmt accrual*
    nav_net_mgmt = (gross_equity - mgmt_accrued) / units if units > 0 else 0.0
    perf_accrued = _accrued_perf_fee(
        nav_net_mgmt, hwm, units, perf_rate, hurdle_rate, days_since_perf
    )

    nav_per_unit_net = ((gross_equity - mgmt_accrued - perf_accrued) / units) if units > 0 else 0.0

    initial_nav = float(ledger_entry.get("initial_nav_per_unit", 1.0))
    return_pct = ((nav_per_unit_net / initial_nav) - 1.0) * 100 if initial_nav > 0 else 0.0
    annualized = 0.0
    if days_since_inception > 0 and initial_nav > 0:
        years = days_since_inception / 365.25
        if years > 0 and nav_per_unit_net > 0:
            annualized = ((nav_per_unit_net / initial_nav) ** (1 / years) - 1.0) * 100

    return NavSnapshot(
        fund_id=fund_id,
        as_of=now.isoformat(),
        capital_usd=round(capital, 4),
        gross_pnl_usd=round(gross_pnl, 4),
        gross_equity_usd=round(gross_equity, 4),
        units_outstanding=round(units, 6),
        nav_per_unit_gross=round(nav_per_unit_gross, 6),
        accrued_mgmt_fee_usd=round(mgmt_accrued, 4),
        accrued_perf_fee_usd=round(perf_accrued, 4),
        nav_per_unit_net=round(nav_per_unit_net, 6),
        hwm_per_unit=round(hwm, 6),
        cumulative_mgmt_fees_paid_usd=round(
            float(ledger_entry.get("cumulative_mgmt_fees_paid_usd", 0.0)), 4
        ),
        cumulative_perf_fees_paid_usd=round(
            float(ledger_entry.get("cumulative_perf_fees_paid_usd", 0.0)), 4
        ),
        return_since_inception_pct=round(return_pct, 4),
        days_since_inception=days_since_inception,
        annualized_return_pct=round(annualized, 3),
    )


# ---------- crystallization ----------


def _crystallize_fee(kind: str, fund_id: str, policy: dict, summary: dict, ledger: dict) -> dict:
    """Move an accrued fee to 'paid'. Returns a delta report dict."""
    snap = compute_nav(fund_id, policy, summary, ledger)
    if snap is None:
        return {"fund_id": fund_id, "kind": kind, "status": "missing"}

    entry = ledger["funds"][fund_id]
    now_iso = _now().isoformat()

    if kind == "mgmt":
        fee_usd = snap.accrued_mgmt_fee_usd
        if fee_usd <= 0:
            entry["last_mgmt_crystallization_date"] = now_iso
            return {"fund_id": fund_id, "kind": kind, "fee_usd": 0.0, "status": "no_accrual"}
        # Management fee is paid in USD — deducted from fund equity by
        # adding to cumulative_mgmt_fees_paid.
        entry["cumulative_mgmt_fees_paid_usd"] = round(
            float(entry.get("cumulative_mgmt_fees_paid_usd", 0.0)) + fee_usd, 6
        )
        entry["last_mgmt_crystallization_date"] = now_iso
        return {"fund_id": fund_id, "kind": kind, "fee_usd": round(fee_usd, 4), "status": "paid"}

    if kind == "perf":
        fee_usd = snap.accrued_perf_fee_usd
        if fee_usd <= 0:
            entry["last_perf_crystallization_date"] = now_iso
            return {"fund_id": fund_id, "kind": kind, "fee_usd": 0.0, "status": "no_accrual"}
        entry["cumulative_perf_fees_paid_usd"] = round(
            float(entry.get("cumulative_perf_fees_paid_usd", 0.0)) + fee_usd, 6
        )
        # HWM resets to new NAV (post-fee, which equals net NAV at this moment)
        entry["hwm_per_unit"] = round(snap.nav_per_unit_net, 6)
        entry["last_perf_crystallization_date"] = now_iso
        return {
            "fund_id": fund_id,
            "kind": kind,
            "fee_usd": round(fee_usd, 4),
            "new_hwm_per_unit": entry["hwm_per_unit"],
            "status": "paid",
        }

    return {"fund_id": fund_id, "kind": kind, "status": "unknown_kind"}


def crystallize(kind: str, fund_id: str | None = None) -> list[dict]:
    """Crystallize mgmt or perf fees across all (or one) fund."""
    policy = _load_json(POLICY_FILE, {})
    summary = _load_json(PORTFOLIO_SUMMARY, {"sleeves": {}})
    ledger = _load_json(NAV_LEDGER, {"funds": {}})

    fund_ids = [fund_id] if fund_id else list(ledger.get("funds", {}).keys())
    deltas = []
    for fid in fund_ids:
        if fid not in ledger.get("funds", {}):
            continue
        deltas.append(_crystallize_fee(kind, fid, policy, summary, ledger))
    _save_json_atomic(NAV_LEDGER, ledger)
    return deltas


# ---------- statement ----------


def _period_bounds(period: str) -> tuple[datetime, datetime]:
    """
    Accepts 'YYYY-MM' (month), 'YYYY-Qn' (quarter), 'YYYY' (annual).
    Returns [start, end_exclusive).
    """
    period = period.strip().upper()
    if len(period) == 4 and period.isdigit():
        year = int(period)
        return (
            datetime(year, 1, 1, tzinfo=UTC),
            datetime(year + 1, 1, 1, tzinfo=UTC),
        )
    if "-Q" in period:
        year_s, q_s = period.split("-Q")
        year, q = int(year_s), int(q_s)
        start_m = (q - 1) * 3 + 1
        end_m = start_m + 3
        start = datetime(year, start_m, 1, tzinfo=UTC)
        if end_m > 12:
            end = datetime(year + 1, 1, 1, tzinfo=UTC)
        else:
            end = datetime(year, end_m, 1, tzinfo=UTC)
        return start, end
    year_s, month_s = period.split("-")
    year, month = int(year_s), int(month_s)
    start = datetime(year, month, 1, tzinfo=UTC)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, month + 1, 1, tzinfo=UTC)
    return start, end


def generate_statement(fund_id: str, period: str) -> dict | None:
    policy = _load_json(POLICY_FILE, {})
    summary = _load_json(PORTFOLIO_SUMMARY, {"sleeves": {}})
    ledger = _load_json(NAV_LEDGER, {"funds": {}})

    snap = compute_nav(fund_id, policy, summary, ledger)
    if snap is None:
        return None
    fund_cfg = policy["funds"][fund_id]
    start, end = _period_bounds(period)

    # Per-sleeve detail for this fund
    sleeves = []
    for sid, s in summary.get("sleeves", {}).items():
        if not sid.startswith(fund_id + "."):
            continue
        sleeve_short = sid.split(".", 1)[1]
        sleeves.append(
            {
                "sleeve": sleeve_short,
                "target_pct": s.get("target_pct"),
                "target_usd": s.get("target_usd"),
                "open_exposure_usd": s.get("open_exposure_usd"),
                "drift_pct": s.get("drift_pct"),
                "pnl_usd": s.get("pnl_usd"),
                "pnl_pct_of_capital": round(
                    float(s.get("pnl_usd") or 0) / float(snap.capital_usd) * 100, 4
                )
                if snap.capital_usd
                else 0,
                "funded": s.get("funded"),
                "positions_total": s.get("positions_total"),
                "resolved": s.get("resolved"),
                "win_rate_pct": s.get("win_rate_pct"),
            }
        )
    sleeves.sort(key=lambda x: -float(x.get("open_exposure_usd") or 0))

    fees = fund_cfg.get("fees", {}) or {}
    return {
        "fund_id": fund_id,
        "name": fund_cfg.get("name", fund_id),
        "statement_period": period,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "as_of": snap.as_of,
        "fund_profile": {
            "target_annual_return_pct": fund_cfg.get("target_annual_return_pct"),
            "max_drawdown_pct": fund_cfg.get("max_drawdown_pct"),
            "payout_cadence": fund_cfg.get("payout_cadence"),
        },
        "fees": {
            "management_fee_annual_pct": fees.get("management_fee_annual_pct"),
            "performance_fee_pct": fees.get("performance_fee_pct"),
            "hurdle_rate_annual_pct": fees.get("hurdle_rate_annual_pct"),
        },
        "nav": {
            "capital_usd": snap.capital_usd,
            "gross_equity_usd": snap.gross_equity_usd,
            "gross_pnl_usd": snap.gross_pnl_usd,
            "units_outstanding": snap.units_outstanding,
            "nav_per_unit_gross": snap.nav_per_unit_gross,
            "nav_per_unit_net": snap.nav_per_unit_net,
            "hwm_per_unit": snap.hwm_per_unit,
            "return_since_inception_pct": snap.return_since_inception_pct,
            "annualized_return_pct": snap.annualized_return_pct,
            "days_since_inception": snap.days_since_inception,
        },
        "fee_accrual": {
            "accrued_mgmt_fee_usd": snap.accrued_mgmt_fee_usd,
            "accrued_perf_fee_usd": snap.accrued_perf_fee_usd,
            "cumulative_mgmt_fees_paid_usd": snap.cumulative_mgmt_fees_paid_usd,
            "cumulative_perf_fees_paid_usd": snap.cumulative_perf_fees_paid_usd,
        },
        "sleeves": sleeves,
    }


# ---------- CLI ----------


def _print_nav(snap: NavSnapshot, fund_name: str = ""):
    print(f"\n=== {fund_name or snap.fund_id} ===")
    print(f"  inception   : {snap.days_since_inception} days ago")
    print(f"  capital     : ${snap.capital_usd:,.2f}")
    print(f"  gross PnL   : ${snap.gross_pnl_usd:+,.4f}")
    print(f"  gross equity: ${snap.gross_equity_usd:,.4f}")
    print(f"  units       : {snap.units_outstanding:,.4f}")
    print(f"  NAV/unit gross : ${snap.nav_per_unit_gross:.6f}")
    print(f"  accrued mgmt fee : ${snap.accrued_mgmt_fee_usd:.4f}")
    print(f"  accrued perf fee : ${snap.accrued_perf_fee_usd:.4f}")
    print(f"  NAV/unit NET   : ${snap.nav_per_unit_net:.6f}  (HWM ${snap.hwm_per_unit:.6f})")
    print(
        f"  return       : {snap.return_since_inception_pct:+.3f}%  "
        f"annualized {snap.annualized_return_pct:+.2f}%"
    )
    print(
        f"  fees paid to date: mgmt ${snap.cumulative_mgmt_fees_paid_usd:.2f}  "
        f"perf ${snap.cumulative_perf_fees_paid_usd:.2f}"
    )


def _print_statement(stmt: dict):
    print(f"\n=== {stmt['name']} — Statement {stmt['statement_period']} ===")
    print(f"Period: {stmt['period_start'][:10]} → {stmt['period_end'][:10]}")
    print(f"Generated: {stmt['as_of']}")
    nav = stmt["nav"]
    fees = stmt["fees"]
    accr = stmt["fee_accrual"]
    print()
    print("  Fund profile:")
    print(f"    target annual return : {stmt['fund_profile']['target_annual_return_pct']}%")
    print(f"    max drawdown budget  : {stmt['fund_profile']['max_drawdown_pct']}%")
    print(f"    payout cadence       : {stmt['fund_profile']['payout_cadence']}")
    print()
    print("  NAV:")
    print(f"    capital           : ${nav['capital_usd']:,.2f}")
    print(f"    gross PnL         : ${nav['gross_pnl_usd']:+,.4f}")
    print(f"    gross equity      : ${nav['gross_equity_usd']:,.4f}")
    print(f"    units outstanding : {nav['units_outstanding']:,.4f}")
    print(f"    NAV/unit gross    : ${nav['nav_per_unit_gross']:.6f}")
    print(f"    NAV/unit NET      : ${nav['nav_per_unit_net']:.6f}")
    print(f"    HWM               : ${nav['hwm_per_unit']:.6f}")
    print(
        f"    return since incep: {nav['return_since_inception_pct']:+.3f}%  "
        f"(annualized {nav['annualized_return_pct']:+.2f}%)"
    )
    print()
    print("  Fees:")
    print(
        f"    mgmt {fees['management_fee_annual_pct']}% annual | "
        f"perf {fees['performance_fee_pct']}% over HWM | "
        f"hurdle {fees['hurdle_rate_annual_pct']}% annual"
    )
    print(
        f"    accrued unpaid    : mgmt ${accr['accrued_mgmt_fee_usd']:.4f}  "
        f"perf ${accr['accrued_perf_fee_usd']:.4f}"
    )
    print(
        f"    cumulative paid   : mgmt ${accr['cumulative_mgmt_fees_paid_usd']:.4f}  "
        f"perf ${accr['cumulative_perf_fees_paid_usd']:.4f}"
    )
    print()
    print("  Sleeve detail (sorted by exposure):")
    print(f"    {'sleeve':<24} {'target':>8} {'open':>10} {'drift':>9} {'PnL':>10} {'% cap':>8}")
    for s in stmt["sleeves"]:
        print(
            f"    {s['sleeve']:<24} {s['target_pct']:>6}%  "
            f"${s['open_exposure_usd']:>8,.2f} {s['drift_pct']:>+7.1f}% "
            f"${s['pnl_usd']:>+8,.2f} {s['pnl_pct_of_capital']:>+6.2f}%"
        )


def main():
    ap = argparse.ArgumentParser(description="Hermes NAV accounting CLI")
    ap.add_argument("--show", action="store_true", help="Print current NAV per fund")
    ap.add_argument(
        "--statement",
        type=str,
        default=None,
        help="Generate statement for period (YYYY-MM, YYYY-Qn, or YYYY)",
    )
    ap.add_argument("--fund", type=str, default=None, help="Restrict to one fund_id (default: all)")
    ap.add_argument(
        "--crystallize-mgmt",
        action="store_true",
        help="Crystallize management fees (moves accrued -> paid)",
    )
    ap.add_argument(
        "--crystallize-perf",
        action="store_true",
        help="Crystallize performance fees (moves accrued -> paid, resets HWM)",
    )
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable text")
    args = ap.parse_args()

    policy = _load_json(POLICY_FILE, {})
    summary = _load_json(PORTFOLIO_SUMMARY, {"sleeves": {}})
    ledger = _load_json(NAV_LEDGER, {"funds": {}})

    fund_ids = [args.fund] if args.fund else list(ledger.get("funds", {}).keys())

    if args.crystallize_mgmt:
        deltas = crystallize("mgmt", args.fund)
        print(json.dumps(deltas, indent=2) if args.json else f"crystallized mgmt: {deltas}")
        return
    if args.crystallize_perf:
        deltas = crystallize("perf", args.fund)
        print(json.dumps(deltas, indent=2) if args.json else f"crystallized perf: {deltas}")
        return

    if args.statement:
        out = []
        for fid in fund_ids:
            stmt = generate_statement(fid, args.statement)
            if stmt is None:
                continue
            out.append(stmt)
            if not args.json:
                _print_statement(stmt)
        if args.json:
            print(json.dumps(out, indent=2))
        return

    if args.show or (
        not args.statement and not args.crystallize_mgmt and not args.crystallize_perf
    ):
        snaps = []
        for fid in fund_ids:
            snap = compute_nav(fid, policy, summary, ledger)
            if snap is None:
                continue
            name = policy.get("funds", {}).get(fid, {}).get("name", fid)
            snaps.append(snap)
            if not args.json:
                _print_nav(snap, name)
        if args.json:
            print(json.dumps([s.__dict__ for s in snaps], indent=2))
        return


if __name__ == "__main__":
    main()
