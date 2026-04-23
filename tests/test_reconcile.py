#!/usr/bin/env python3
"""
Unit tests for scripts/reconcile.py — book integrity, passport hash,
on-chain drift detection with mocked web3.

Runs standalone with just stdlib (no pytest required):
    python3 tests/test_reconcile.py
"""
from __future__ import annotations
import hashlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import reconcile  # noqa: E402


SAMPLE_PAYLOAD = {
    "agent_id": "test-agent",
    "capabilities": ["scan", "settle"],
    "chain_id": 2368,
    "controller": "0xabc",
    "protocol": "did:test",
    "version": "v0.1.0",
}


def _good_settled(n=3):
    """Return a well-formed settled dict with n txs."""
    sleeves = [f"fund_x.sleeve_{i}" for i in range(n)]
    hashes = {s: ("a" * 64)[:64] for s in sleeves}
    # Distinct hashes per tx so hashes-vs-txs check passes
    txs = []
    for i, s in enumerate(sleeves):
        content = hashlib.sha256(f"{s}:{i}".encode()).hexdigest()
        tx = hashlib.sha256(f"tx:{i}".encode()).hexdigest()
        hashes[s] = content
        txs.append({
            "sleeve": s,
            "nonce": 9 + i,
            "content_hash": content,
            "tx": tx,
        })
    return {"hashes": hashes, "txs": txs}


def _good_registry():
    payload_json = json.dumps(SAMPLE_PAYLOAD, sort_keys=True, separators=(",", ":"))
    return {
        "agent_id": SAMPLE_PAYLOAD["agent_id"],
        "payload": SAMPLE_PAYLOAD,
        "payload_hash": hashlib.sha256(payload_json.encode()).hexdigest(),
    }


# ---------- book checks ----------

class TestBookIntegrity(unittest.TestCase):
    def test_clean_book_passes(self):
        report = reconcile.Report(timestamp="", wallet="", rpc="",
                                  onchain_checked=False, book_tx_count=0)
        reconcile.check_book_integrity(_good_settled(3), report)
        errors = [f for f in report.findings if f.severity == "error"]
        self.assertEqual(errors, [])

    def test_duplicate_nonce_detected(self):
        settled = _good_settled(3)
        settled["txs"][1]["nonce"] = settled["txs"][0]["nonce"]  # force dupe
        report = reconcile.Report(timestamp="", wallet="", rpc="",
                                  onchain_checked=False, book_tx_count=0)
        reconcile.check_book_integrity(settled, report)
        errors = [f for f in report.findings if f.severity == "error"]
        self.assertTrue(any("duplicate nonces" in f.message for f in errors))

    def test_malformed_content_hash_detected(self):
        settled = _good_settled(3)
        settled["txs"][0]["content_hash"] = "not-a-real-hash"
        report = reconcile.Report(timestamp="", wallet="", rpc="",
                                  onchain_checked=False, book_tx_count=0)
        reconcile.check_book_integrity(settled, report)
        errors = [f for f in report.findings if f.severity == "error"]
        self.assertTrue(any("content_hash" in f.message for f in errors))

    def test_nonce_gaps_warn_not_error(self):
        settled = _good_settled(3)
        settled["txs"][2]["nonce"] = 99  # introduces a gap
        report = reconcile.Report(timestamp="", wallet="", rpc="",
                                  onchain_checked=False, book_tx_count=0)
        reconcile.check_book_integrity(settled, report)
        warns = [f for f in report.findings if f.severity == "warn"]
        errors = [f for f in report.findings if f.severity == "error"]
        self.assertTrue(any("non-contiguous" in f.message for f in warns))
        self.assertEqual(errors, [])


class TestHashesVsTxs(unittest.TestCase):
    def test_all_sleeves_have_matching_tx(self):
        report = reconcile.Report(timestamp="", wallet="", rpc="",
                                  onchain_checked=False, book_tx_count=0)
        reconcile.check_hashes_vs_txs(_good_settled(3), report)
        errors = [f for f in report.findings if f.severity == "error"]
        self.assertEqual(errors, [])

    def test_orphan_sleeve_flagged(self):
        settled = _good_settled(3)
        settled["hashes"]["fund_x.orphan"] = "z" * 64   # no matching tx
        report = reconcile.Report(timestamp="", wallet="", rpc="",
                                  onchain_checked=False, book_tx_count=0)
        reconcile.check_hashes_vs_txs(settled, report)
        errors = [f for f in report.findings if f.severity == "error"]
        self.assertTrue(any("no matching tx" in f.message for f in errors))


# ---------- passport ----------

class TestPassport(unittest.TestCase):
    def test_clean_passport_matches(self):
        report = reconcile.Report(timestamp="", wallet="", rpc="",
                                  onchain_checked=False, book_tx_count=0)
        reconcile.check_passport_hash(_good_registry(), report)
        errors = [f for f in report.findings if f.severity == "error"]
        self.assertEqual(errors, [])

    def test_drift_detected_when_payload_mutated(self):
        """Mutating the payload after hash was stored should be caught."""
        reg = _good_registry()
        reg["payload"]["agent_id"] = "mutated"
        report = reconcile.Report(timestamp="", wallet="", rpc="",
                                  onchain_checked=False, book_tx_count=0)
        reconcile.check_passport_hash(reg, report)
        errors = [f for f in report.findings if f.severity == "error"]
        self.assertTrue(any("drift" in f.message for f in errors))

    def test_missing_hash_warns(self):
        reg = {"payload": SAMPLE_PAYLOAD}  # no payload_hash
        report = reconcile.Report(timestamp="", wallet="", rpc="",
                                  onchain_checked=False, book_tx_count=0)
        reconcile.check_passport_hash(reg, report)
        warns = [f for f in report.findings if f.severity == "warn"]
        self.assertTrue(any("stored payload_hash" in f.message for f in warns))

    def test_register_agent_serialization_compatible(self):
        """The hash we recompute must match register_agent.py's format."""
        from json import dumps
        # This replicates the exact line in onchain/register_agent.py L69:
        payload_json = dumps(SAMPLE_PAYLOAD, sort_keys=True, separators=(",", ":"))
        expected = hashlib.sha256(payload_json.encode()).hexdigest()
        reg = {"payload": SAMPLE_PAYLOAD, "payload_hash": expected}
        report = reconcile.Report(timestamp="", wallet="", rpc="",
                                  onchain_checked=False, book_tx_count=0)
        reconcile.check_passport_hash(reg, report)
        errors = [f for f in report.findings if f.severity == "error"]
        self.assertEqual(errors, [])


# ---------- on-chain (mocked web3) ----------

class TestOnchain(unittest.TestCase):
    def _mk_w3(self, nonce: int, txs: dict):
        """Build a mocked web3 instance; txs maps tx_hash -> from address."""
        w3 = MagicMock()
        w3.is_connected.return_value = True
        w3.eth.get_transaction_count.return_value = nonce

        def _get_tx(h):
            key = h.lower().replace("0x", "")
            if key not in txs:
                raise Exception("tx not found")
            return {"from": txs[key]}
        w3.eth.get_transaction.side_effect = _get_tx
        return w3

    def _patch_web3(self, w3):
        fake_module = types.ModuleType("web3")
        fake_module.Web3 = MagicMock(return_value=w3)
        return patch.dict(sys.modules, {"web3": fake_module})

    def test_nonce_aligned_passes(self):
        settled = _good_settled(3)
        # max book nonce = 11 (9, 10, 11); on-chain = 12
        wallet = "0xabc"
        txs_by_hash = {t["tx"]: wallet for t in settled["txs"]}
        w3 = self._mk_w3(nonce=12, txs=txs_by_hash)
        report = reconcile.Report(timestamp="", wallet=wallet, rpc="",
                                  onchain_checked=False, book_tx_count=3)
        with self._patch_web3(w3):
            reconcile.check_onchain(settled, wallet, "http://x", report)
        errors = [f for f in report.findings if f.severity == "error"]
        self.assertEqual(errors, [])
        # one ok message for the nonce
        self.assertTrue(any(f.category == "nonce" and f.severity == "ok"
                            for f in report.findings))

    def test_nonce_higher_onchain_flags_missing_book_tx(self):
        settled = _good_settled(3)
        wallet = "0xabc"
        txs_by_hash = {t["tx"]: wallet for t in settled["txs"]}
        w3 = self._mk_w3(nonce=20, txs=txs_by_hash)  # 20 - 11 - 1 = 8 missing
        report = reconcile.Report(timestamp="", wallet=wallet, rpc="",
                                  onchain_checked=False, book_tx_count=3)
        with self._patch_web3(w3):
            reconcile.check_onchain(settled, wallet, "http://x", report)
        errors = [f for f in report.findings if f.severity == "error"]
        self.assertTrue(any("missing from book" in f.message for f in errors))

    def test_nonce_lower_onchain_flags_fictional_tx(self):
        """Book claims txs that aren't on-chain."""
        settled = _good_settled(3)
        wallet = "0xabc"
        txs_by_hash = {t["tx"]: wallet for t in settled["txs"]}
        w3 = self._mk_w3(nonce=10, txs=txs_by_hash)  # < book
        report = reconcile.Report(timestamp="", wallet=wallet, rpc="",
                                  onchain_checked=False, book_tx_count=3)
        with self._patch_web3(w3):
            reconcile.check_onchain(settled, wallet, "http://x", report)
        errors = [f for f in report.findings if f.severity == "error"]
        self.assertTrue(any("aren't on-chain" in f.message for f in errors))

    def test_tx_from_wrong_wallet_detected(self):
        settled = _good_settled(1)
        wallet = "0xabc"
        wrong = "0xDEF"
        txs_by_hash = {settled["txs"][0]["tx"]: wrong}
        w3 = self._mk_w3(nonce=10, txs=txs_by_hash)
        report = reconcile.Report(timestamp="", wallet=wallet, rpc="",
                                  onchain_checked=False, book_tx_count=1)
        with self._patch_web3(w3):
            reconcile.check_onchain(settled, wallet, "http://x", report)
        errors = [f for f in report.findings if f.severity == "error"]
        self.assertTrue(any("not from expected wallet" in f.message for f in errors))

    def test_rpc_unreachable_warns_not_errors(self):
        w3 = MagicMock()
        w3.is_connected.return_value = False
        report = reconcile.Report(timestamp="", wallet="0x", rpc="",
                                  onchain_checked=False, book_tx_count=0)
        with self._patch_web3(w3):
            reconcile.check_onchain({"txs": []}, "0x", "http://bad", report)
        errors = [f for f in report.findings if f.severity == "error"]
        warns = [f for f in report.findings if f.severity == "warn"]
        self.assertEqual(errors, [])
        self.assertTrue(any("unreachable" in f.message for f in warns))


class TestReportExit(unittest.TestCase):
    def test_clean_report_reports_clean(self):
        r = reconcile.Report(timestamp="", wallet="", rpc="",
                             onchain_checked=True, book_tx_count=1)
        r.add("ok", "book", "fine")
        r.add("warn", "x", "just a warn")
        self.assertTrue(r.clean)
        self.assertEqual(r.error_count, 0)

    def test_any_error_marks_unclean(self):
        r = reconcile.Report(timestamp="", wallet="", rpc="",
                             onchain_checked=True, book_tx_count=1)
        r.add("error", "book", "bad")
        self.assertFalse(r.clean)
        self.assertEqual(r.error_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
