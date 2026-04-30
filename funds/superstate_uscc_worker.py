#!/usr/bin/env python3
"""
superstate_uscc -- third leg of 75/25 stablecoin_yield triad alongside
aave_usdc and sgho.

Mechanism: HOLD USCC (Superstate Short Duration US Government Securities Fund).
Tokenized on-chain exposure to short-duration Treasuries issued by Superstate.
Different risk surface than DeFi lending (regulated NAV fund vs smart-contract
pool), so completes diversification across smart-contract / DeFi-stable /
regulated-treasury legs in the balanced fund.

APY source: Superstate publishes NAV daily; we fetch the reference yield
via DeFiLlama's mirrored pool. If DeFiLlama doesn't cover it yet, the
worker falls back to Superstate's public API shape and surfaces the error.

Shared engine: yield_base.run_yield(cfg).
"""

from __future__ import annotations

import json
import urllib.request

from policy import sleeve_targets_for
from yield_base import YieldConfig, run_yield

WORKER_NAME = "superstate_uscc"

# Superstate publishes fund NAV + net yield on a public API. Schema:
#   https://api.superstate.co/v1/funds/uscc  ->  {"net_yield_30d": "4.82", ...}
SUPERSTATE_API = "https://api.superstate.co/v1/funds/uscc"


def fetch_uscc_apy() -> tuple[float | None, str]:
    req = urllib.request.Request(SUPERSTATE_API, headers={"User-Agent": "hermes-superstate-uscc/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception as e:
        return None, f"superstate api: {e}"
    # Schema accepts a few common field names across versions.
    for key in ("net_yield_30d", "net_yield", "apy", "yield_30d"):
        v = data.get(key)
        if v is None:
            continue
        try:
            return float(v) / 100.0, data.get("as_of", "")
        except (TypeError, ValueError):
            continue
    return None, ""


_FALLBACK_TARGETS = {"fund_75_25_balanced.stablecoin_yield": 83.33}

CONFIG = YieldConfig(
    worker_name=WORKER_NAME,
    symbol="SUPERSTATE_USCC",
    sleeve_targets=sleeve_targets_for(WORKER_NAME) or _FALLBACK_TARGETS,
    apy_fetcher=fetch_uscc_apy,
    protocol="superstate-uscc",
    chain="ethereum",
    asset="USCC",
    counterparty="superstate_regulated_nav",
    direction="HOLD",
    confidence=0.96,
)


if __name__ == "__main__":
    run_yield(CONFIG)
