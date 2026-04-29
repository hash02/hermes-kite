#!/usr/bin/env python3
"""
Unit tests for funds/nav_accounting.py — fee accrual, HWM, crystallization,
statement generation, period parsing.

Runs standalone with just stdlib (no pytest required):
    python3 tests/test_nav_accounting.py
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import nav_accounting as nav  # noqa: E402


def _policy(mgmt_pct=1.0, perf_pct=20.0, hurdle_pct=0.0, capital=1000.0):
    return {
        "funds": {
            "fund_t": {
                "name": "Test Fund",
                "capital_usd": capital,
                "target_annual_return_pct": 10.0,
                "max_drawdown_pct": 10.0,
                "payout_cadence": "quarterly",
                "fees": {
                    "management_fee_annual_pct": mgmt_pct,
                    "performance_fee_pct": perf_pct,
                    "hurdle_rate_annual_pct": hurdle_pct,
                },
                "sleeves": {
                    "only": {"target_pct": 100, "workers": {}},
                },
            }
        }
    }


def _ledger(days_since_inception=50, hwm=1.0, units=1000.0, mgmt_paid=0.0, perf_paid=0.0):
    inception = datetime.now(UTC)
    inception = inception.replace(hour=0, minute=0, second=0, microsecond=0)
    inception = inception.fromtimestamp(
        inception.timestamp() - days_since_inception * 86400, tz=UTC
    )
    iso = inception.isoformat()
    return {
        "funds": {
            "fund_t": {
                "inception_date": iso,
                "initial_capital_usd": 1000.0,
                "initial_nav_per_unit": 1.0,
                "units_outstanding": units,
                "hwm_per_unit": hwm,
                "last_mgmt_crystallization_date": iso,
                "cumulative_mgmt_fees_paid_usd": mgmt_paid,
                "last_perf_crystallization_date": iso,
                "cumulative_perf_fees_paid_usd": perf_paid,
            }
        }
    }


def _summary(pnl: float) -> dict:
    return {"sleeves": {"fund_t.only": {"pnl_usd": pnl}}}


class TestMgmtFee(unittest.TestCase):
    def test_accrual_linear_in_days(self):
        """1% annual on $1000 over 365 days should accrue exactly $10."""
        fee = nav._accrued_mgmt_fee(1000.0, 1.0, 365)
        self.assertAlmostEqual(fee, 10.0, places=4)

    def test_accrual_zero_when_no_time(self):
        self.assertEqual(nav._accrued_mgmt_fee(1000.0, 1.0, 0), 0.0)

    def test_accrual_zero_when_no_rate(self):
        self.assertEqual(nav._accrued_mgmt_fee(1000.0, 0.0, 100), 0.0)

    def test_accrual_scales_with_equity(self):
        # Equity 2x -> fee 2x
        a = nav._accrued_mgmt_fee(1000.0, 2.0, 30)
        b = nav._accrued_mgmt_fee(2000.0, 2.0, 30)
        self.assertAlmostEqual(b, 2 * a, places=4)


class TestPerfFee(unittest.TestCase):
    def test_zero_when_below_hwm(self):
        fee = nav._accrued_perf_fee(
            nav_per_unit=0.99,
            hwm_per_unit=1.0,
            units=1000,
            perf_rate=20.0,
            hurdle_rate_annual=0.0,
            days_since_crystallization=90,
        )
        self.assertEqual(fee, 0.0)

    def test_linear_above_hwm(self):
        """NAV $1.02 vs HWM $1.00, 1000 units, 20% rate -> 0.02 * 1000 * 0.2 = 4.0."""
        fee = nav._accrued_perf_fee(
            nav_per_unit=1.02,
            hwm_per_unit=1.0,
            units=1000,
            perf_rate=20.0,
            hurdle_rate_annual=0.0,
            days_since_crystallization=90,
        )
        self.assertAlmostEqual(fee, 4.0, places=4)

    def test_hurdle_shields_gains_below_hurdle(self):
        """Hurdle 8% annual over 365 days -> effective HWM = 1.08; NAV 1.05 -> no fee."""
        fee = nav._accrued_perf_fee(
            nav_per_unit=1.05,
            hwm_per_unit=1.0,
            units=1000,
            perf_rate=20.0,
            hurdle_rate_annual=8.0,
            days_since_crystallization=365,
        )
        self.assertEqual(fee, 0.0)

    def test_fee_charged_only_on_excess_over_hurdle(self):
        """NAV 1.15, hurdle-lifted HWM 1.08 -> excess 0.07 * 1000 * 0.2 = 14.0."""
        fee = nav._accrued_perf_fee(
            nav_per_unit=1.15,
            hwm_per_unit=1.0,
            units=1000,
            perf_rate=20.0,
            hurdle_rate_annual=8.0,
            days_since_crystallization=365,
        )
        self.assertAlmostEqual(fee, 14.0, places=4)


class TestComputeNav(unittest.TestCase):
    def test_zero_pnl_returns_par_nav(self):
        """No PnL -> NAV/unit gross equals initial."""
        snap = nav.compute_nav(
            "fund_t",
            _policy(mgmt_pct=0, perf_pct=0),
            _summary(0.0),
            _ledger(days_since_inception=0),
        )
        self.assertAlmostEqual(snap.nav_per_unit_gross, 1.0, places=4)
        self.assertAlmostEqual(snap.nav_per_unit_net, 1.0, places=4)

    def test_positive_pnl_lifts_nav(self):
        snap = nav.compute_nav(
            "fund_t",
            _policy(mgmt_pct=0, perf_pct=0),
            _summary(50.0),
            _ledger(days_since_inception=0),
        )
        self.assertAlmostEqual(snap.nav_per_unit_gross, 1.05, places=4)

    def test_mgmt_fee_reduces_net_nav(self):
        """1% annual mgmt over 365 days on $1000 -> $10 net equity loss -> NAV drops $0.01."""
        snap = nav.compute_nav(
            "fund_t",
            _policy(mgmt_pct=1.0, perf_pct=0),
            _summary(0.0),
            _ledger(days_since_inception=365),
        )
        self.assertAlmostEqual(snap.accrued_mgmt_fee_usd, 10.0, places=2)
        self.assertAlmostEqual(snap.nav_per_unit_net, 0.99, places=3)

    def test_perf_fee_only_on_outperformance(self):
        """NAV 1.05 vs HWM 1.00, 20% perf, no hurdle -> fee 1000 (ish)."""
        snap = nav.compute_nav(
            "fund_t",
            _policy(mgmt_pct=0, perf_pct=20.0, hurdle_pct=0),
            _summary(50.0),
            _ledger(days_since_inception=30, hwm=1.0),
        )
        # gross NAV/unit = 1050/1000 = 1.05; perf fee = 0.05 * 1000 * 0.2 = 10
        self.assertAlmostEqual(snap.accrued_perf_fee_usd, 10.0, places=2)


class TestCrystallize(unittest.TestCase):
    def test_mgmt_crystallization_increments_cumulative_paid(self):
        policy = _policy(mgmt_pct=1.0, perf_pct=0)
        summary = _summary(0.0)
        ledger = _ledger(days_since_inception=365)

        report = nav._crystallize_fee("mgmt", "fund_t", policy, summary, ledger)
        self.assertEqual(report["status"], "paid")
        self.assertAlmostEqual(report["fee_usd"], 10.0, places=1)
        # Ledger mutated: paid now ~$10
        self.assertAlmostEqual(
            ledger["funds"]["fund_t"]["cumulative_mgmt_fees_paid_usd"],
            10.0,
            places=1,
        )

    def test_perf_crystallization_resets_hwm(self):
        policy = _policy(mgmt_pct=0, perf_pct=20.0, hurdle_pct=0)
        summary = _summary(50.0)
        ledger = _ledger(days_since_inception=30, hwm=1.0)

        report = nav._crystallize_fee("perf", "fund_t", policy, summary, ledger)
        self.assertEqual(report["status"], "paid")
        self.assertGreater(report["fee_usd"], 0)
        # HWM should have moved up to NAV (post-fee)
        new_hwm = ledger["funds"]["fund_t"]["hwm_per_unit"]
        self.assertGreater(new_hwm, 1.0)
        self.assertLess(new_hwm, 1.05)  # below gross NAV because fee deducted

    def test_perf_crystallization_noop_below_hwm(self):
        policy = _policy(mgmt_pct=0, perf_pct=20.0, hurdle_pct=0)
        summary = _summary(-10.0)
        ledger = _ledger(days_since_inception=30, hwm=1.05)
        report = nav._crystallize_fee("perf", "fund_t", policy, summary, ledger)
        self.assertEqual(report["status"], "no_accrual")
        # HWM unchanged
        self.assertEqual(ledger["funds"]["fund_t"]["hwm_per_unit"], 1.05)


class TestPeriodParsing(unittest.TestCase):
    def test_month(self):
        start, end = nav._period_bounds("2026-04")
        self.assertEqual(start, datetime(2026, 4, 1, tzinfo=UTC))
        self.assertEqual(end, datetime(2026, 5, 1, tzinfo=UTC))

    def test_december_month(self):
        start, end = nav._period_bounds("2026-12")
        self.assertEqual(start, datetime(2026, 12, 1, tzinfo=UTC))
        self.assertEqual(end, datetime(2027, 1, 1, tzinfo=UTC))

    def test_quarter(self):
        start, end = nav._period_bounds("2026-Q2")
        self.assertEqual(start, datetime(2026, 4, 1, tzinfo=UTC))
        self.assertEqual(end, datetime(2026, 7, 1, tzinfo=UTC))

    def test_q4_wraps_year(self):
        start, end = nav._period_bounds("2026-Q4")
        self.assertEqual(start, datetime(2026, 10, 1, tzinfo=UTC))
        self.assertEqual(end, datetime(2027, 1, 1, tzinfo=UTC))

    def test_annual(self):
        start, end = nav._period_bounds("2026")
        self.assertEqual(start, datetime(2026, 1, 1, tzinfo=UTC))
        self.assertEqual(end, datetime(2027, 1, 1, tzinfo=UTC))


class TestStatement(unittest.TestCase):
    def test_statement_shape(self):
        """generate_statement uses loaded policy/summary/ledger — here we test
        via the low-level path with monkey-patched file readers."""
        from unittest.mock import patch

        with patch.object(
            nav,
            "_load_json",
            side_effect=[
                _policy(),
                _summary(12.0),
                _ledger(days_since_inception=30),
            ],
        ):
            stmt = nav.generate_statement("fund_t", "2026-04")
        self.assertEqual(stmt["fund_id"], "fund_t")
        self.assertEqual(stmt["statement_period"], "2026-04")
        self.assertIn("nav", stmt)
        self.assertIn("fees", stmt)
        self.assertIn("sleeves", stmt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
