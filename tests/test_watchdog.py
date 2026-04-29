#!/usr/bin/env python3
"""Unit tests for scripts/watchdog.py.

Runs standalone (no pytest):
    python3 -m unittest tests.test_watchdog
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import watchdog  # noqa: E402


def _write_status(tmp_dir: Path, name: str, heartbeat: datetime, **extra) -> Path:
    """Create a status JSON file with the given last_heartbeat."""
    path = tmp_dir / f"{name}.json"
    payload = {
        "worker_name": name,
        "status": "active",
        "last_heartbeat": heartbeat.isoformat(),
        **extra,
    }
    path.write_text(json.dumps(payload))
    return path


class TestWatchdog(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fresh_status_passes(self) -> None:
        now = datetime.now(UTC)
        _write_status(self.tmp_dir, "aave_usdc", now - timedelta(minutes=5))
        report = watchdog.run(self.tmp_dir, max_age_minutes=60, expected_workers=["aave_usdc"])
        self.assertTrue(report.clean)
        self.assertEqual(report.workers_seen, 1)
        self.assertTrue(any(f.category == "fresh" for f in report.findings))

    def test_stale_status_flagged(self) -> None:
        now = datetime.now(UTC)
        _write_status(self.tmp_dir, "delta_neutral_funding", now - timedelta(minutes=120))
        report = watchdog.run(
            self.tmp_dir, max_age_minutes=60, expected_workers=["delta_neutral_funding"]
        )
        self.assertFalse(report.clean)
        self.assertEqual(report.error_count, 1)
        finding = next(f for f in report.findings if f.severity == "error")
        self.assertEqual(finding.category, "stale")
        self.assertEqual(finding.worker, "delta_neutral_funding")

    def test_missing_worker_flagged(self) -> None:
        # Expected has 2 workers, only 1 has a status file
        now = datetime.now(UTC)
        _write_status(self.tmp_dir, "aave_usdc", now - timedelta(minutes=5))
        report = watchdog.run(
            self.tmp_dir,
            max_age_minutes=60,
            expected_workers=["aave_usdc", "morpho_usdc"],
        )
        self.assertFalse(report.clean)
        missing = [f for f in report.findings if f.category == "missing"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].worker, "morpho_usdc")

    def test_malformed_json_flagged(self) -> None:
        bad = self.tmp_dir / "broken.json"
        bad.write_text("{ this is not json")
        report = watchdog.run(self.tmp_dir, max_age_minutes=60, expected_workers=["broken"])
        self.assertFalse(report.clean)
        self.assertTrue(any(f.category == "malformed" for f in report.findings))

    def test_missing_heartbeat_field_flagged(self) -> None:
        path = self.tmp_dir / "no_heartbeat.json"
        path.write_text(json.dumps({"worker_name": "no_heartbeat", "status": "active"}))
        report = watchdog.run(self.tmp_dir, max_age_minutes=60, expected_workers=["no_heartbeat"])
        self.assertFalse(report.clean)
        finding = next(f for f in report.findings if f.severity == "error")
        self.assertEqual(finding.category, "malformed")
        self.assertIn("missing last_heartbeat", finding.message)

    def test_missing_status_dir_flagged(self) -> None:
        nonexistent = self.tmp_dir / "does-not-exist"
        report = watchdog.run(nonexistent, max_age_minutes=60, expected_workers=[])
        self.assertFalse(report.clean)
        self.assertTrue(any("does not exist" in f.message for f in report.findings))

    def test_naive_datetime_treated_as_utc(self) -> None:
        """Status files with naive ISO timestamps should be treated as UTC,
        not flagged as malformed."""
        path = self.tmp_dir / "naive.json"
        # naive ISO (no offset)
        ts = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
        path.write_text(json.dumps({"worker_name": "naive", "last_heartbeat": ts}))
        report = watchdog.run(self.tmp_dir, max_age_minutes=60, expected_workers=["naive"])
        self.assertTrue(report.clean)

    def test_clean_property_and_error_count(self) -> None:
        r = watchdog.Report(timestamp="", status_dir="", max_age_minutes=60)
        r.add("ok", "fresh", "fine")
        self.assertTrue(r.clean)
        self.assertEqual(r.error_count, 0)
        r.add("error", "stale", "bad")
        self.assertFalse(r.clean)
        self.assertEqual(r.error_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
