#!/usr/bin/env python3
"""
Unit tests for funds/risk_engine.py.

Runs standalone with just stdlib (no pytest required):
    python3 tests/test_risk_engine.py

Each test builds a synthetic policy + portfolio + summary and asserts the
engine's sizing math. No filesystem or network — all inputs are in-memory.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import risk_engine  # noqa: E402


def _policy(
    engine_enabled=True,
    kelly=0.25,
    target_vol=8.0,
    max_cp_pct=30.0,
    dd_halt=None,
    fund_capital=1000.0,
    n_sleeves_in_fund=4,
):
    sleeves = {f"sleeve_{i}": {"target_pct": 25, "workers": {}} for i in range(n_sleeves_in_fund)}
    return {
        "funds": {
            "fund_test": {
                "capital_usd": fund_capital,
                "sleeves": sleeves,
            }
        },
        "risk": {
            "engine_enabled": engine_enabled,
            "kelly_fraction": kelly,
            "target_portfolio_vol_pct": target_vol,
            "max_concentration_per_counterparty_pct": max_cp_pct,
            "max_drawdown_halt_per_fund_pct": dd_halt,
        },
    }


class TestApplyEngine(unittest.TestCase):
    def test_passthrough_when_disabled(self):
        """engine_enabled=false -> static values pass through unchanged."""
        with patch.object(risk_engine, "_load_policy", return_value=_policy(engine_enabled=False)):
            out = risk_engine.apply_engine("aave_usdc", {"fund_test.sleeve_0": 133.34})
            self.assertEqual(out, {"fund_test.sleeve_0": 133.34})

    def test_kelly_scale_only_when_no_vol_cap(self):
        """Low-vol sleeve: kelly is the only binding constraint."""
        policy = _policy(engine_enabled=True, kelly=0.25, target_vol=8.0, n_sleeves_in_fund=4)
        with (
            patch.object(risk_engine, "_load_policy", return_value=policy),
            patch.object(risk_engine, "_load_live_portfolio", return_value=[]),
            patch.object(risk_engine, "_load_summary", return_value={"sleeves": {}}),
        ):
            # aave_usdc category=yield -> bootstrap vol = 1.0%
            # per_sleeve_vol_budget = 8 / sqrt(4) = 4.0
            # vol_cap = 1000 * 4.0 / 1.0 = $4000
            # base = min(100, 4000) = 100
            # sized = 100 * 0.25 = 25
            out = risk_engine.apply_engine("aave_usdc", {"fund_test.sleeve_0": 100.0})
            self.assertAlmostEqual(out["fund_test.sleeve_0"], 25.0, places=4)

    def test_vol_cap_binds_for_high_vol_worker(self):
        """High-vol sleeve: vol cap binds before kelly."""
        # memecoin_sniper category=memecoin -> bootstrap 80%
        policy = _policy(engine_enabled=True, kelly=0.25, target_vol=8.0, n_sleeves_in_fund=4)
        with (
            patch.object(risk_engine, "_load_policy", return_value=policy),
            patch.object(risk_engine, "_load_live_portfolio", return_value=[]),
            patch.object(risk_engine, "_load_summary", return_value={"sleeves": {}}),
        ):
            # per_sleeve_vol_budget = 8 / sqrt(4) = 4.0
            # vol_cap = 1000 * 4.0 / 80.0 = $50
            # base = min(300, 50) = 50
            # sized = 50 * 0.25 = 12.5
            out = risk_engine.apply_engine("crypto_memecoins", {"fund_test.sleeve_0": 300.0})
            self.assertAlmostEqual(out["fund_test.sleeve_0"], 12.5, places=4)

    def test_drawdown_halt_zeros_size(self):
        """Fund in drawdown worse than halt threshold -> size is zero."""
        policy = _policy(
            engine_enabled=True, kelly=0.25, target_vol=8.0, dd_halt=5.0, fund_capital=1000.0
        )
        # Build a summary where the fund has -$100 PnL = -10% drawdown
        summary = {"sleeves": {"fund_test.sleeve_0": {"pnl_usd": -100.0}}}
        with (
            patch.object(risk_engine, "_load_policy", return_value=policy),
            patch.object(risk_engine, "_load_live_portfolio", return_value=[]),
            patch.object(risk_engine, "_load_summary", return_value=summary),
        ):
            out = risk_engine.apply_engine("aave_usdc", {"fund_test.sleeve_0": 100.0})
            self.assertEqual(out["fund_test.sleeve_0"], 0.0)

    def test_drawdown_halt_doesnt_trigger_when_above_threshold(self):
        """Fund at -3% drawdown with 5% halt threshold -> halt not triggered."""
        policy = _policy(
            engine_enabled=True, kelly=0.25, target_vol=8.0, dd_halt=5.0, fund_capital=1000.0
        )
        summary = {"sleeves": {"fund_test.sleeve_0": {"pnl_usd": -30.0}}}
        with (
            patch.object(risk_engine, "_load_policy", return_value=policy),
            patch.object(risk_engine, "_load_live_portfolio", return_value=[]),
            patch.object(risk_engine, "_load_summary", return_value=summary),
        ):
            out = risk_engine.apply_engine("aave_usdc", {"fund_test.sleeve_0": 100.0})
            self.assertGreater(out["fund_test.sleeve_0"], 0.0)


class TestRealizedVol(unittest.TestCase):
    def test_bootstrap_when_insufficient_history(self):
        """Fewer than 5 resolved positions -> uses bootstrap value for category."""
        positions = [
            {
                "worker": "aave_usdc",
                "sleeve": "fund_test.sleeve_0",
                "resolved": True,
                "principal_usd": 100,
                "pnl_usd": 2.0,
            },
        ]  # only 1 resolved -> below threshold of 5
        vol = risk_engine.realized_vol_pct("fund_test.sleeve_0", "aave_usdc", positions)
        self.assertEqual(vol, risk_engine._BOOTSTRAP_VOL_PCT["yield"])

    def test_computed_vol_with_enough_history(self):
        """≥5 resolved -> compute std from actuals."""
        # 6 trades with identical +1% pnl -> std 0 -> vol 0.01 (floor)
        positions = [
            {
                "worker": "tv_momentum",
                "sleeve": "fund_test.sleeve_0",
                "resolved": True,
                "principal_usd": 100,
                "pnl_usd": 1.0,
            }
            for _ in range(6)
        ]
        vol = risk_engine.realized_vol_pct("fund_test.sleeve_0", "tv_momentum", positions)
        self.assertAlmostEqual(vol, 0.01, places=3)  # floor

    def test_computed_vol_reflects_spread(self):
        """Spread in pnl -> non-trivial vol."""
        # Alternating +5% / -5% on $100 principal = 0.05 stdev
        # Annualized: 0.05 * sqrt(365) * 100 = ~95.5%
        positions = [
            {
                "worker": "tv_momentum",
                "sleeve": "fund_test.sleeve_0",
                "resolved": True,
                "principal_usd": 100,
                "pnl_usd": 5.0 if i % 2 == 0 else -5.0,
            }
            for i in range(10)
        ]
        vol = risk_engine.realized_vol_pct("fund_test.sleeve_0", "tv_momentum", positions)
        self.assertGreater(vol, 80.0)
        self.assertLess(vol, 110.0)


class TestDrawdown(unittest.TestCase):
    def test_positive_pnl_positive_drawdown_pct(self):
        policy = _policy(fund_capital=1000.0)
        summary = {
            "sleeves": {
                "fund_test.sleeve_0": {"pnl_usd": 50.0},
                "fund_test.sleeve_1": {"pnl_usd": -20.0},
            }
        }
        with patch.object(risk_engine, "_load_policy", return_value=policy):
            dd = risk_engine.fund_drawdown_pct("fund_test", summary)
        self.assertAlmostEqual(dd, 3.0, places=3)  # (50 - 20) / 1000 * 100

    def test_only_this_funds_sleeves_counted(self):
        """Sleeves from other funds must not bleed into the calc."""
        policy = _policy(fund_capital=1000.0)
        summary = {
            "sleeves": {
                "fund_test.sleeve_0": {"pnl_usd": 10.0},
                "fund_other.sleeve_0": {"pnl_usd": -500.0},  # should not count
            }
        }
        with patch.object(risk_engine, "_load_policy", return_value=policy):
            dd = risk_engine.fund_drawdown_pct("fund_test", summary)
        self.assertAlmostEqual(dd, 1.0, places=3)


class TestConcentration(unittest.TestCase):
    def test_exposure_sums_per_counterparty(self):
        policy = {
            "funds": {
                "f1": {
                    "capital_usd": 1000,
                    "sleeves": {
                        "s1": {
                            "workers": {
                                "aave_usdc": {"principal_usd": 100},
                                "sgho": {"principal_usd": 50},
                            }
                        },
                        "s2": {
                            "workers": {
                                "morpho_usdc": {"principal_usd": 200},
                            }
                        },
                    },
                }
            }
        }
        exposures = risk_engine.counterparty_exposure_pct("f1", policy)
        self.assertAlmostEqual(exposures["aave_v3"], 10.0, places=3)
        self.assertAlmostEqual(exposures["aave_savings"], 5.0, places=3)
        self.assertAlmostEqual(exposures["morpho_blue"], 20.0, places=3)

    def test_concentration_scalar_applies(self):
        """Counterparty over cap -> size scaled down by cap/current ratio."""
        # Contrive: two aave_usdc positions total $400 in a $1000 fund = 40% > 30% cap
        # scalar = 30/40 = 0.75 -> sized dropped by 25%
        policy = {
            "funds": {
                "f1": {
                    "capital_usd": 1000,
                    "sleeves": {
                        "s1": {"workers": {"aave_usdc": {"principal_usd": 200}}},
                        "s2": {"workers": {"aave_usdc": {"principal_usd": 200}}},
                        "s3": {"workers": {"aave_usdc": {"principal_usd": 0}}},
                        "s4": {"workers": {}},
                    },
                }
            },
            "risk": {
                "engine_enabled": True,
                "kelly_fraction": 1.0,  # no Kelly discount, isolate the concentration effect
                "target_portfolio_vol_pct": 1000.0,  # huge -> vol cap does not bind
                "max_concentration_per_counterparty_pct": 30.0,
            },
        }
        with (
            patch.object(risk_engine, "_load_policy", return_value=policy),
            patch.object(risk_engine, "_load_live_portfolio", return_value=[]),
            patch.object(risk_engine, "_load_summary", return_value={"sleeves": {}}),
        ):
            out = risk_engine.apply_engine("aave_usdc", {"f1.s1": 200.0})
            # 200 * (30/40) = 150
            self.assertAlmostEqual(out["f1.s1"], 150.0, places=2)


class TestPolicyIntegration(unittest.TestCase):
    def test_sleeve_targets_for_passthrough_when_engine_off(self):
        """Integration: policy.sleeve_targets_for returns static when engine disabled."""
        # Use real policy.json — engine should be off by default
        from engine import policy as policy_mod

        policy_mod._load_policy.cache_clear()
        raw = policy_mod._load_policy()
        if raw.get("risk", {}).get("engine_enabled"):
            self.skipTest("engine is enabled in repo policy; test needs it off")
        out = policy_mod.sleeve_targets_for("aave_usdc")
        static = policy_mod._static_sleeve_targets_for("aave_usdc")
        self.assertEqual(out, static)


if __name__ == "__main__":
    unittest.main(verbosity=2)
