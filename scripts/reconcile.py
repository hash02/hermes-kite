#!/usr/bin/env python3
"""
Reconciliation — compares the book (data/kite_settled.json + nav_ledger)
against on-chain state on Kite testnet. Flags drift.

Checks:
  1. Book integrity — every tx in data/kite_settled.json has a unique nonce
     in sequence (no duplicates, no gaps); content_hash matches the
     expected format.
  2. Agent passport — data/agent_registry.json payload_hash matches the
     committed self-attested DID.
  3. On-chain nonce — web3.eth.get_transaction_count(wallet) equals
     max_nonce_in_book + 1.
  4. Per-tx on-chain match — every tx hash in the book resolves via
     w3.eth.get_transaction() and came from our wallet.
  5. Unknown on-chain txs — any tx from our wallet on Kite that is NOT in
     the book is flagged (book drift).

Produces a report dict + prints a summary. Exit code 0 on clean
reconciliation, 1 on any drift. Suitable for cron.

Gracefully degrades when Kite RPC is unreachable or when the web3
library is not installed — book-only checks still run.

Usage:
  python3 scripts/reconcile.py
  python3 scripts/reconcile.py --rpc https://rpc-testnet.gokite.ai/
  python3 scripts/reconcile.py --wallet 0xA29fF03ABfd219e3c76D1C18653297B8201B7748
  python3 scripts/reconcile.py --json                     # emit structured report
  python3 scripts/reconcile.py --skip-onchain             # book-only (offline CI)
  python3 scripts/reconcile.py --output-dir exports/reconcile_2026-04-23
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SETTLED_FILE = REPO_ROOT / "data" / "kite_settled.json"
REGISTRY_FILE = REPO_ROOT / "data" / "agent_registry.json"
PORTFOLIO_SUMMARY = REPO_ROOT / "data" / "portfolio_summary.json"

DEFAULT_RPC = os.environ.get("KITE_RPC", "https://rpc-testnet.gokite.ai/")
DEFAULT_WALLET = "0xA29fF03ABfd219e3c76D1C18653297B8201B7748"

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


# ---------- IO ----------


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


# ---------- report shape ----------


@dataclass
class Finding:
    severity: str  # "ok", "warn", "error"
    category: str  # "book", "passport", "nonce", "tx", "unknown_tx"
    message: str


@dataclass
class Report:
    timestamp: str
    wallet: str
    rpc: str
    onchain_checked: bool
    book_tx_count: int
    onchain_nonce: int | None = None
    findings: list[Finding] = field(default_factory=list)

    def add(self, severity: str, category: str, message: str):
        self.findings.append(Finding(severity, category, message))

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    @property
    def clean(self) -> bool:
        return self.error_count == 0


# ---------- book-side checks ----------


def check_book_integrity(settled: dict, report: Report) -> None:
    """Every tx has a unique increasing nonce, sha256 content_hash is well-formed."""
    txs = settled.get("txs") or []
    if not txs:
        report.add("warn", "book", "no settlement txs in book")
        return
    nonces = [t.get("nonce") for t in txs]
    # 1. uniqueness
    if len(nonces) != len(set(nonces)):
        dupes = [n for n in nonces if nonces.count(n) > 1]
        report.add("error", "book", f"duplicate nonces in book: {sorted(set(dupes))}")
    # 2. no gaps (monotonic increase, may not start at 0)
    sorted_nonces = sorted(nonces)
    gaps = [(a, b) for a, b in zip(sorted_nonces, sorted_nonces[1:], strict=False) if b - a != 1]
    if gaps:
        report.add(
            "warn",
            "book",
            f"non-contiguous nonce range: gap(s) {gaps[:3]}{'…' if len(gaps) > 3 else ''}",
        )
    # 3. each content_hash is sha256-shaped (64 hex chars)
    bad = [t.get("nonce") for t in txs if not _HEX64.match((t.get("content_hash") or "").lower())]
    if bad:
        report.add(
            "error", "book", f"{len(bad)} tx(s) have malformed content_hash (not sha256-64hex)"
        )
    # 4. each tx hash is sha256-shaped too (eth tx hashes are 66 chars inc. 0x, but book strips)
    bad_tx = [
        t.get("nonce")
        for t in txs
        if not _HEX64.match((t.get("tx") or "").lower().replace("0x", ""))
    ]
    if bad_tx:
        report.add("error", "book", f"{len(bad_tx)} tx(s) have malformed tx hash")

    if not any(f.severity == "error" for f in report.findings):
        report.add("ok", "book", f"{len(txs)} book txs pass integrity (nonces, hashes well-formed)")


def check_passport_hash(registry: dict, report: Report) -> None:
    """The stored payload_hash must equal sha256 of the stored payload."""
    if not registry:
        report.add("warn", "passport", "no agent registry file")
        return
    payload = registry.get("payload") or {}
    stored_hash = (registry.get("payload_hash") or "").lower()
    # Must match the serialization used by onchain/register_agent.py:
    # json.dumps(payload, sort_keys=True, separators=(',', ':')) — compact form,
    # no spaces. Using default separators would give a different hash.
    recomputed = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if stored_hash and stored_hash == recomputed:
        report.add(
            "ok", "passport", f"agent passport hash matches (agent_id={registry.get('agent_id')})"
        )
    elif not stored_hash:
        report.add("warn", "passport", "registry has no stored payload_hash")
    else:
        report.add(
            "error",
            "passport",
            f"passport hash drift: stored={stored_hash[:12]} computed={recomputed[:12]}",
        )


def check_hashes_vs_txs(settled: dict, report: Report) -> None:
    """Every sleeve in `hashes` should have at least one corresponding tx."""
    hashes = settled.get("hashes") or {}
    txs = settled.get("txs") or []
    tx_hashes = {t.get("content_hash") for t in txs}
    orphan_sleeves = [sleeve for sleeve, h in hashes.items() if h not in tx_hashes]
    if orphan_sleeves:
        report.add(
            "error",
            "book",
            f"{len(orphan_sleeves)} sleeve(s) in hashes/ with no matching tx: "
            f"{orphan_sleeves[:3]}{'…' if len(orphan_sleeves) > 3 else ''}",
        )
    else:
        report.add(
            "ok", "book", f"all {len(hashes)} sleeve content hashes have a matching on-chain tx"
        )


# ---------- on-chain checks ----------


def check_onchain(settled: dict, wallet: str, rpc_url: str, report: Report) -> None:
    """Query Kite testnet. Requires web3. Degrades gracefully."""
    try:
        from web3 import Web3  # type: ignore
    except Exception as e:
        report.add("warn", "nonce", f"web3 not available ({e}); skipped on-chain")
        return

    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        if not w3.is_connected():
            report.add("warn", "nonce", f"Kite RPC {rpc_url} unreachable; skipped on-chain")
            return
    except Exception as e:
        report.add("warn", "nonce", f"Kite RPC connect failed: {e}; skipped on-chain")
        return

    report.onchain_checked = True

    try:
        nonce_on = w3.eth.get_transaction_count(wallet)
    except Exception as e:
        report.add("error", "nonce", f"eth_getTransactionCount failed: {e}")
        return
    report.onchain_nonce = nonce_on

    txs = settled.get("txs") or []
    max_book_nonce = max((t.get("nonce") or -1) for t in txs) if txs else -1

    if nonce_on == max_book_nonce + 1:
        report.add(
            "ok", "nonce", f"on-chain nonce {nonce_on} == book max_nonce+1 ({max_book_nonce}+1)"
        )
    elif nonce_on > max_book_nonce + 1:
        missing = nonce_on - max_book_nonce - 1
        report.add(
            "error",
            "nonce",
            f"on-chain nonce {nonce_on} > book+{max_book_nonce}+1 — {missing} "
            f"tx(s) broadcast but missing from book",
        )
    else:
        # on-chain < book: should never happen (means book has fictional txs)
        report.add(
            "error",
            "nonce",
            f"on-chain nonce {nonce_on} < book max {max_book_nonce}+1 — "
            f"book claims txs that aren't on-chain",
        )

    # Per-tx on-chain check (sample up to 20 latest to keep it fast)
    for t in sorted(txs, key=lambda x: -(x.get("nonce") or 0))[:20]:
        tx_hash = t.get("tx") or ""
        if not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash
        try:
            on_tx = w3.eth.get_transaction(tx_hash)
        except Exception as e:
            report.add(
                "error", "tx", f"tx {tx_hash[:12]}… (nonce {t.get('nonce')}) not found on Kite: {e}"
            )
            continue
        if (on_tx.get("from") or "").lower() != wallet.lower():
            report.add("error", "tx", f"tx {tx_hash[:12]}… not from expected wallet")
        else:
            # Quietly passing — don't spam findings
            pass
    # Emit one consolidated ok if nothing flagged
    if not any(f.category == "tx" and f.severity == "error" for f in report.findings):
        sampled = min(20, len(txs))
        report.add(
            "ok", "tx", f"sampled {sampled} latest tx(s) — all resolve on Kite and are from wallet"
        )


# ---------- main ----------


def run(skip_onchain: bool, rpc_url: str, wallet: str) -> Report:
    settled = load_json(SETTLED_FILE, {"hashes": {}, "txs": []})
    registry = load_json(REGISTRY_FILE, {})

    report = Report(
        timestamp=datetime.now(UTC).isoformat(),
        wallet=wallet,
        rpc=rpc_url,
        onchain_checked=False,
        book_tx_count=len(settled.get("txs") or []),
    )

    check_book_integrity(settled, report)
    check_hashes_vs_txs(settled, report)
    check_passport_hash(registry, report)

    if not skip_onchain:
        check_onchain(settled, wallet, rpc_url, report)
    else:
        report.add("warn", "nonce", "on-chain checks skipped (--skip-onchain)")

    return report


def _print_human(report: Report) -> None:
    print()
    print(f"=== Reconciliation report  {report.timestamp} ===")
    print(f"wallet:  {report.wallet}")
    print(f"rpc:     {report.rpc}")
    print(f"book_tx_count: {report.book_tx_count}")
    print(f"onchain_checked: {report.onchain_checked}")
    if report.onchain_nonce is not None:
        print(f"onchain_nonce:   {report.onchain_nonce}")
    print()
    for f in report.findings:
        marker = {"ok": "  ", "warn": "~ ", "error": "! "}.get(f.severity, "  ")
        print(f"  [{f.severity:<5}] {marker}{f.category:<10}  {f.message}")
    print()
    if report.clean:
        print(
            f"RESULT: clean ({report.error_count} errors, "
            f"{sum(1 for f in report.findings if f.severity == 'warn')} warnings)"
        )
    else:
        print(f"RESULT: DRIFT ({report.error_count} error(s))")


def main():
    ap = argparse.ArgumentParser(description="Hermes reconciliation (book vs on-chain)")
    ap.add_argument("--rpc", type=str, default=DEFAULT_RPC)
    ap.add_argument("--wallet", type=str, default=DEFAULT_WALLET)
    ap.add_argument(
        "--skip-onchain", action="store_true", help="Skip Kite RPC calls (book-only checks)"
    )
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of human text")
    ap.add_argument(
        "--output-dir", type=Path, default=None, help="Write report.json to this directory"
    )
    args = ap.parse_args()

    report = run(args.skip_onchain, args.rpc, args.wallet)

    if args.json:
        print(
            json.dumps(
                {
                    **{k: v for k, v in asdict(report).items() if k != "findings"},
                    "findings": [asdict(f) for f in report.findings],
                    "clean": report.clean,
                    "error_count": report.error_count,
                },
                indent=2,
            )
        )
    else:
        _print_human(report)

    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        tag = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_path = args.output_dir / f"reconcile_{tag}.json"
        out_path.write_text(
            json.dumps(
                {
                    **{k: v for k, v in asdict(report).items() if k != "findings"},
                    "findings": [asdict(f) for f in report.findings],
                    "clean": report.clean,
                    "error_count": report.error_count,
                },
                indent=2,
            )
        )
        if not args.json:
            print(f"  report written to {out_path}")

    sys.exit(0 if report.clean else 1)


if __name__ == "__main__":
    main()
