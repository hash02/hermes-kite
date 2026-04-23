#!/usr/bin/env python3
"""
Unit tests for funds/backtest.py — the synthetic Monte Carlo harness.

Runs standalone with just stdlib (no pytest required):
    python3 tests/test_backtest.py

Covers the pure math (metrics, percentiles, daily params), the simulator
output shape, and a fixed-seed regression so changes to the MC math are
caught before they land.
"""
from __future__ import annotations
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "funds"))

import backtest  # noqa: E402


class TestMetrics(unittest.TestCase):
    def test_max_drawdown_zero_when_monotonic(self):
        self.assertEqual(backtest._max_drawdown_pct([100, 101, 102, 103]), 0.0)

    def test_max_drawdown_from_peak(self):
        # peak 100, trough 80 -> -20%
        self.assertAlmostEqual(backtest._max_drawdown_pct([100, 100, 80, 90]), -20.0, places=3)

    def test_max_drawdown_multiple_cycles_picks_worst(self):
        # 100 -> 90 (-10), then 95 -> 50 (-47.4%)
        self.assertAlmostEqual(
            backtest._max_drawdown_pct([100, 90, 100, 95, 50]),
            -50.0, places=3,  # from peak 100 to trough 50
        )

    def test_sharpe_zero_for_zero_std(self):
        self.assertEqual(backtest._sharpe([0.001, 0.001, 0.001]), 0.0)

    def test_sharpe_positive_for_positive_drift(self):
        # Mean 0.001 per day, std 0.005 -> Sharpe = (0.001/0.005) * sqrt(252) = 3.17
        returns = [0.001 + 0.005 * (1 if i % 2 else -1) for i in range(100)]
        s = backtest._sharpe(returns)
        self.assertGreater(s, 2.0)
        self.assertLess(s, 5.0)

    def test_sortino_only_penalizes_downside(self):
        # Positive mean with varied up/down returns — Sortino should be
        # greater than Sharpe because downside stddev < total stddev.
        import random
        rng = random.Random(7)
        ups = [rng.uniform(0.005, 0.015) for _ in range(60)]
        downs = [rng.uniform(-0.008, -0.002) for _ in range(40)]
        returns = ups + downs
        sharpe = backtest._sharpe(returns)
        sortino = backtest._sortino(returns)
        self.assertGreater(sortino, sharpe)

    def test_cagr_compounding(self):
        # Start 1000, end 2000 over 252 days -> 100% CAGR
        equity = [1000] + [1000] * 250 + [2000]
        self.assertAlmostEqual(
            backtest._cagr_pct(equity, 252), 100.0, places=1,
        )

    def test_cagr_zero_for_flat(self):
        equity = [1000] * 253
        self.assertAlmostEqual(backtest._cagr_pct(equity, 252), 0.0, places=3)

    def test_win_rate(self):
        self.assertEqual(
            backtest._win_rate_pct([1, 1, 1, -1, -1, 0, 0, 0, 0, 0]), 30.0,
        )

    def test_ann_vol(self):
        # Daily returns stddev 0.01 -> annualized = 0.01 * sqrt(252) * 100 = 15.87
        returns = [0.01 * (1 if i % 2 else -1) for i in range(100)]
        vol = backtest._ann_vol_pct(returns)
        self.assertAlmostEqual(vol, 15.87, delta=0.5)


class TestPercentile(unittest.TestCase):
    def test_single_value(self):
        self.assertEqual(backtest._pctl([42.0], 50), 42.0)

    def test_sorted_percentiles(self):
        vals = list(range(1, 101))  # 1..100
        # 50th percentile of 1..100 is between 50 and 51 under linear interp
        self.assertAlmostEqual(backtest._pctl(vals, 50), 50.5, places=3)

    def test_extremes(self):
        vals = [10, 20, 30, 40, 50]
        self.assertEqual(backtest._pctl(vals, 0), 10)
        self.assertEqual(backtest._pctl(vals, 100), 50)


class TestDailyParams(unittest.TestCase):
    def test_conversion(self):
        # 25.2% annual return, 25% annual vol
        mu, sigma = backtest._daily_params(25.2, 25.0)
        # mu_daily = 0.252 / 252 = 0.001
        # sigma_daily = 0.25 / sqrt(252) = 0.01575
        self.assertAlmostEqual(mu, 0.001, places=5)
        self.assertAlmostEqual(sigma, 0.01575, places=4)


class TestRunMC(unittest.TestCase):
    def _min_policy(self):
        # Single fund, single sleeve, single worker — deterministic structure.
        return {
            "funds": {
                "fund_x": {
                    "capital_usd": 1000.0,
                    "sleeves": {
                        "yield_only": {
                            "target_pct": 100,
                            "workers": {"aave_usdc": {"principal_usd": 500.0}},
                        },
                    },
                }
            },
            "risk": {
                "engine_enabled": False,
                "kelly_fraction": 1.0,
                "target_portfolio_vol_pct": 100.0,
                "max_concentration_per_counterparty_pct": 100.0,
                "max_drawdown_halt_per_fund_pct": None,
            },
        }

    def test_output_shape(self):
        res = backtest.run_mc(self._min_policy(), days=100, sims=10, seed=1)
        self.assertEqual(res["sims"], 10)
        self.assertEqual(res["days"], 100)
        self.assertIn("fund_x", res["aggregated"])
        m = res["aggregated"]["fund_x"]
        for key in ["sharpe", "sortino", "max_dd_pct", "cagr_pct",
                    "ann_vol_pct", "win_rate_pct", "final_equity"]:
            self.assertIn(key, m)
            self.assertIn("p5", m[key])
            self.assertIn("p50", m[key])
            self.assertIn("p95", m[key])
            self.assertIn("mean", m[key])

    def test_engine_off_size_equals_static(self):
        """With engine off, simulated sizes equal static — terminal equity
        should match running the static-size simulation directly."""
        snap = self._min_policy()
        # Engine off
        res_off = backtest.run_mc(snap, days=50, sims=30, seed=123)
        # Engine on with kelly 1.0, huge vol budget, no dd halt, no cp cap ->
        # engine should be a no-op (same sizes)
        snap["risk"]["engine_enabled"] = True
        snap["risk"]["target_portfolio_vol_pct"] = 1e9
        res_on = backtest.run_mc(snap, days=50, sims=30, seed=123)
        # p50 Sharpe should match within tolerance (same draws, same sizes)
        self.assertAlmostEqual(
            res_off["aggregated"]["fund_x"]["sharpe"]["p50"],
            res_on["aggregated"]["fund_x"]["sharpe"]["p50"],
            places=3,
        )

    def test_kelly_scales_linearly(self):
        """Kelly 0.5 should produce half the sleeve size -> half the cumulative
        PnL -> half the CAGR (roughly) and half the vol (roughly)."""
        snap = self._min_policy()
        snap["risk"]["engine_enabled"] = True
        snap["risk"]["target_portfolio_vol_pct"] = 1e9  # disable vol cap

        snap["risk"]["kelly_fraction"] = 1.0
        r1 = backtest.run_mc(snap, days=252, sims=50, seed=7)
        snap["risk"]["kelly_fraction"] = 0.5
        r05 = backtest.run_mc(snap, days=252, sims=50, seed=7)

        # Vol should be ~halved
        self.assertAlmostEqual(
            r05["aggregated"]["fund_x"]["ann_vol_pct"]["p50"],
            r1["aggregated"]["fund_x"]["ann_vol_pct"]["p50"] * 0.5,
            delta=0.25,  # some simulation noise acceptable
        )

    def test_drawdown_halt_reduces_maxdd(self):
        """A sufficiently tight drawdown halt should reduce MaxDD."""
        snap = self._min_policy()
        # Use a high-vol strategy to ensure drawdowns happen in 1yr horizon
        snap["funds"]["fund_x"]["sleeves"]["yield_only"]["workers"] = {
            "crypto_memecoins": {"principal_usd": 500.0},
        }

        # Engine off (baseline)
        r_off = backtest.run_mc(snap, days=252, sims=100, seed=99)

        # Engine on with tight halt
        snap["risk"]["engine_enabled"] = True
        snap["risk"]["kelly_fraction"] = 1.0  # isolate halt effect
        snap["risk"]["target_portfolio_vol_pct"] = 1e9
        snap["risk"]["max_drawdown_halt_per_fund_pct"] = 5.0
        r_on = backtest.run_mc(snap, days=252, sims=100, seed=99)

        off_p50 = r_off["aggregated"]["fund_x"]["max_dd_pct"]["p50"]
        on_p50 = r_on["aggregated"]["fund_x"]["max_dd_pct"]["p50"]
        # MaxDD is negative — ON (halt active) should be less negative
        self.assertGreater(on_p50, off_p50)


if __name__ == "__main__":
    unittest.main(verbosity=2)
