#!/usr/bin/env python3
"""
On-demand CSV exporter.

Reads the committed snapshot + live portfolio + settlement ledger and
emits five CSV files for ops / investor reporting / spreadsheet drop-in.

Inputs:
  data/portfolio_summary.json        aggregated sleeve view (from cron)
  data/kite_settled.json             on-chain settlement markers
  ~/.hermes/brain/paper_portfolio.json  per-position detail (live; optional)
  config/policy.json (via funds/policy.py) — fund allocations + knobs

Outputs (default ./exports/):
  funds.csv           per-fund aggregates (capital, open, staked, pnl, coverage)
  sleeves.csv         per-sleeve aggregates (target, open, drift, positions, pnl)
  positions.csv       every individual position (open + resolved)
  trades.csv          resolved positions only (for a trade log)
  settlements.csv     on-chain markers (nonce, sleeve, tx, content_hash)

Usage:
  python3 scripts/export_csv.py
  python3 scripts/export_csv.py --output-dir exports/2026-04-23
  python3 scripts/export_csv.py --fund fund_75_25_balanced
  python3 scripts/export_csv.py --since 2026-04-20T00:00:00Z
  python3 scripts/export_csv.py --quiet
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SUMMARY_FILE = REPO_ROOT / "data" / "portfolio_summary.json"
SETTLED_FILE = REPO_ROOT / "data" / "kite_settled.json"
LIVE_PORTFOLIO = Path.home() / ".hermes" / "brain" / "paper_portfolio.json"
DEFAULT_OUTPUT = REPO_ROOT / "exports"

# engine.policy is optional — CSV export works on raw JSON without it.
# Self-bootstrap when the script is run directly without `pip install -e .`.
sys.path.insert(0, str(REPO_ROOT))
try:
    from engine import policy  # type: ignore
except Exception:  # pragma: no cover
    policy = None


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def epoch_to_iso(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).isoformat()
    except Exception:
        return str(ts)


def sleeve_fund(sleeve_id: str) -> str:
    return sleeve_id.split(".", 1)[0] if "." in sleeve_id else ""


def _fund_capital(fund_id: str, default: float = 1000.0) -> float:
    if policy is None:
        return default
    return float(policy.fund_cfg(fund_id).get("capital_usd", default))


# ---------- writers ----------


def write_funds_csv(out: Path, summary: dict, quiet: bool):
    sleeves = summary.get("sleeves", {})
    fund_agg = defaultdict(
        lambda: {
            "target_usd": 0.0,
            "open_exposure_usd": 0.0,
            "staked_usd": 0.0,
            "pnl_usd": 0.0,
            "positions_total": 0,
            "resolved": 0,
            "funded_sleeves": 0,
            "total_sleeves": 0,
        }
    )
    for sid, s in sleeves.items():
        fid = sleeve_fund(sid)
        agg = fund_agg[fid]
        agg["target_usd"] += float(s.get("target_usd") or 0)
        agg["open_exposure_usd"] += float(s.get("open_exposure_usd") or 0)
        agg["staked_usd"] += float(s.get("staked_usd") or 0)
        agg["pnl_usd"] += float(s.get("pnl_usd") or 0)
        agg["positions_total"] += int(s.get("positions_total") or 0)
        agg["resolved"] += int(s.get("resolved") or 0)
        agg["total_sleeves"] += 1
        if s.get("funded"):
            agg["funded_sleeves"] += 1

    rows = []
    for fid, a in sorted(fund_agg.items()):
        cap = _fund_capital(fid)
        name = ""
        if policy is not None:
            name = policy.fund_cfg(fid).get("name", "")
        coverage = (a["funded_sleeves"] / a["total_sleeves"] * 100) if a["total_sleeves"] else 0
        rows.append(
            {
                "fund_id": fid,
                "name": name,
                "capital_usd": round(cap, 2),
                "target_usd": round(a["target_usd"], 2),
                "open_exposure_usd": round(a["open_exposure_usd"], 2),
                "staked_usd": round(a["staked_usd"], 2),
                "pnl_usd": round(a["pnl_usd"], 2),
                "positions_total": a["positions_total"],
                "resolved": a["resolved"],
                "funded_sleeves": a["funded_sleeves"],
                "total_sleeves": a["total_sleeves"],
                "coverage_pct": round(coverage, 1),
            }
        )

    path = out / "funds.csv"
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["fund_id"])
        writer.writeheader()
        writer.writerows(rows)
    if not quiet:
        print(f"  wrote {path} ({len(rows)} rows)")


def write_sleeves_csv(out: Path, summary: dict, fund_filter: str | None, quiet: bool):
    rows = []
    for sid, s in summary.get("sleeves", {}).items():
        fid = sleeve_fund(sid)
        if fund_filter and fid != fund_filter:
            continue
        rows.append(
            {
                "sleeve_id": sid,
                "fund_id": fid,
                "sleeve": sid.split(".", 1)[1] if "." in sid else sid,
                "target_pct": s.get("target_pct"),
                "target_usd": s.get("target_usd"),
                "open_exposure_usd": s.get("open_exposure_usd"),
                "drift_pct": s.get("drift_pct"),
                "positions_total": s.get("positions_total"),
                "resolved": s.get("resolved"),
                "win_rate_pct": s.get("win_rate_pct"),
                "pnl_usd": s.get("pnl_usd"),
                "staked_usd": s.get("staked_usd"),
                "funded": s.get("funded"),
                "workers_configured": "|".join(s.get("workers_configured") or []),
                "workers_shipping": "|".join(s.get("workers_shipping") or []),
            }
        )
    rows.sort(key=lambda r: (r["fund_id"], r["sleeve"]))

    path = out / "sleeves.csv"
    with path.open("w", newline="") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        else:
            f.write("sleeve_id\n")
    if not quiet:
        print(f"  wrote {path} ({len(rows)} rows)")


def _position_rows(live: dict, fund_filter: str | None, since: datetime | None):
    raw = live.get("positions") if isinstance(live, dict) else live
    if not isinstance(raw, list):
        return []
    rows = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        fund_id = p.get("fund") or ""
        sleeve = p.get("sleeve") or ""
        if fund_filter and fund_id and fund_id != fund_filter:
            continue
        entry_ts = p.get("entry_time")
        entry_iso = entry_ts if isinstance(entry_ts, str) else epoch_to_iso(entry_ts)
        if since:
            et = parse_iso(entry_iso)
            if et and et < since:
                continue
        resolve_ts = p.get("resolve_time")
        resolve_iso = resolve_ts if isinstance(resolve_ts, str) else epoch_to_iso(resolve_ts)
        rows.append(
            {
                "id": p.get("id", ""),
                "worker": p.get("worker", ""),
                "fund_id": fund_id,
                "sleeve_id": sleeve,
                "symbol": p.get("symbol", ""),
                "direction": p.get("direction", p.get("side", "")),
                "entry_price": p.get("entry_price"),
                "mark_price": p.get("mark_price") or p.get("last_price"),
                "exit_price": p.get("exit_price"),
                "size_usd": p.get("size_usd"),
                "principal_usd": p.get("principal_usd"),
                "pnl_usd": p.get("pnl_usd"),
                "pnl_pct": p.get("pnl_pct"),
                "resolved": bool(p.get("resolved")),
                "correct": p.get("correct"),
                "resolve_reason": p.get("resolve_reason", ""),
                "entry_time": entry_iso,
                "resolve_time": resolve_iso,
            }
        )
    rows.sort(key=lambda r: (r["fund_id"], r["sleeve_id"], r["entry_time"]))
    return rows


def write_positions_csv(
    out: Path, live: dict, fund_filter: str | None, since: datetime | None, quiet: bool
):
    rows = _position_rows(live, fund_filter, since)
    path = out / "positions.csv"
    fields = [
        "id",
        "worker",
        "fund_id",
        "sleeve_id",
        "symbol",
        "direction",
        "entry_price",
        "mark_price",
        "exit_price",
        "size_usd",
        "principal_usd",
        "pnl_usd",
        "pnl_pct",
        "resolved",
        "correct",
        "resolve_reason",
        "entry_time",
        "resolve_time",
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fields})
    if not quiet:
        print(f"  wrote {path} ({len(rows)} rows)")


def write_trades_csv(
    out: Path, live: dict, fund_filter: str | None, since: datetime | None, quiet: bool
):
    rows = [r for r in _position_rows(live, fund_filter, since) if r["resolved"]]
    path = out / "trades.csv"
    fields = [
        "id",
        "worker",
        "fund_id",
        "sleeve_id",
        "symbol",
        "direction",
        "entry_price",
        "exit_price",
        "principal_usd",
        "size_usd",
        "pnl_usd",
        "pnl_pct",
        "correct",
        "resolve_reason",
        "entry_time",
        "resolve_time",
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fields})
    if not quiet:
        print(f"  wrote {path} ({len(rows)} rows)")


def write_settlements_csv(out: Path, settled: dict, fund_filter: str | None, quiet: bool):
    txs = settled.get("txs") or []
    rows = []
    for t in txs:
        sleeve = t.get("sleeve", "")
        fid = sleeve_fund(sleeve)
        if fund_filter and fid and fid != fund_filter:
            continue
        rows.append(
            {
                "nonce": t.get("nonce"),
                "sleeve_id": sleeve,
                "fund_id": fid,
                "tx_hash": t.get("tx", ""),
                "content_hash": t.get("content_hash", ""),
            }
        )
    rows.sort(key=lambda r: r.get("nonce") or 0)
    path = out / "settlements.csv"
    fields = ["nonce", "sleeve_id", "fund_id", "tx_hash", "content_hash"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    if not quiet:
        print(f"  wrote {path} ({len(rows)} rows)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Directory to write CSV files (default: ./exports/)",
    )
    ap.add_argument(
        "--fund",
        type=str,
        default=None,
        help="Filter to a single fund_id (e.g. fund_75_25_balanced)",
    )
    ap.add_argument(
        "--since",
        type=str,
        default=None,
        help="ISO timestamp — only include positions/trades at or after this time",
    )
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    since = parse_iso(args.since) if args.since else None

    summary = load_json(SUMMARY_FILE, {"sleeves": {}})
    settled = load_json(SETTLED_FILE, {"hashes": {}, "txs": []})
    live = load_json(LIVE_PORTFOLIO, {"positions": []})

    if not args.quiet:
        print(f"[export_csv] writing to {out}")
        if args.fund:
            print(f"[export_csv] filter: fund={args.fund}")
        if since:
            print(f"[export_csv] filter: since={since.isoformat()}")

    write_funds_csv(out, summary, args.quiet)
    write_sleeves_csv(out, summary, args.fund, args.quiet)
    write_positions_csv(out, live, args.fund, since, args.quiet)
    write_trades_csv(out, live, args.fund, since, args.quiet)
    write_settlements_csv(out, settled, args.fund, args.quiet)

    # Drop a tiny manifest for reconciliation
    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_snapshot_as_of": summary.get("as_of"),
        "fund_filter": args.fund,
        "since": args.since,
        "inputs": {
            "portfolio_summary": str(SUMMARY_FILE),
            "kite_settled": str(SETTLED_FILE),
            "live_portfolio": str(LIVE_PORTFOLIO),
            "live_portfolio_exists": LIVE_PORTFOLIO.exists(),
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    if not args.quiet:
        print(f"  wrote {out / 'manifest.json'}")


if __name__ == "__main__":
    main()
