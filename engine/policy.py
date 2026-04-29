#!/usr/bin/env python3
"""
Policy loader — single entry point for every knob that used to live in a
worker constant. Workers call the helpers below instead of hardcoding.

File: config/policy.json (at repo root). See config/README.md for schema.
Missing / malformed file -> every helper returns a safe default (usually an
empty dict), letting workers fall back to their built-in values.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

_DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "config" / "policy.json"


def _policy_path() -> Path:
    override = os.environ.get("HERMES_POLICY_PATH")
    if override:
        return Path(override)
    return _DEFAULT_POLICY_PATH


@lru_cache(maxsize=1)
def _load_policy() -> dict:
    p = _policy_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        # Malformed or unreadable policy file. Fall back to empty so workers
        # use their built-in defaults; CI's json-validate hook catches malformed
        # commits before they land.
        return {}


def reload() -> None:
    """Drop the lru cache — use this in tests or long-running processes."""
    _load_policy.cache_clear()


# ---------- fund / sleeve lookup ----------


def fund_cfg(fund_id: str) -> dict:
    return _load_policy().get("funds", {}).get(fund_id, {})


def sleeve_cfg(fund_id: str, sleeve_id: str) -> dict:
    return fund_cfg(fund_id).get("sleeves", {}).get(sleeve_id, {})


def all_fund_ids() -> list[str]:
    return list(_load_policy().get("funds", {}).keys())


# ---------- worker lookup ----------


def worker_cfg(worker_name: str) -> dict:
    """Return the per-worker knobs block (not allocation — those live under funds.*)."""
    return _load_policy().get("workers", {}).get(worker_name, {})


def sleeve_targets_for(worker_name: str) -> dict:
    """
    Return {"<fund_id>.<sleeve_id>": principal_usd} for every sleeve the
    given worker is wired into across all funds.

    When `risk.engine_enabled` is true in policy.json, the raw static values
    are routed through funds.risk_engine.apply_engine() which may override
    them with vol-targeted / Kelly-scaled / drawdown-halted sizes. When
    disabled (default), the static values pass through unchanged.

    Value priority per-sleeve (first non-null wins):
      principal_usd > target_deployment_usd > principal_usd_per_symbol
      (only one is expected; the fallback order lets a single config key
      cover any worker style.)
    """
    static = _static_sleeve_targets_for(worker_name)
    if not risk_engine_enabled():
        return static
    # Lazy import — avoids a circular dependency at module load.
    try:
        from engine.risk_engine import apply_engine  # type: ignore[no-redef]
    except ImportError:
        return static
    try:
        return apply_engine(worker_name, static)
    except Exception:  # noqa: BLE001 — engine failure must not break the worker
        # Engine bug should not take a worker offline; fall back to static
        # sizing and let the next reconcile run flag the inconsistency.
        return static


def _static_sleeve_targets_for(worker_name: str) -> dict:
    """Raw lookup in policy.json with no engine dispatch. Used by risk_engine
    and by sleeve_targets_for when the engine is disabled."""
    out = {}
    policy = _load_policy()
    for fund_id, fund in policy.get("funds", {}).items():
        for sleeve_id, sleeve in fund.get("sleeves", {}).items():
            worker_entry = sleeve.get("workers", {}).get(worker_name)
            if not worker_entry:
                continue
            val = (
                worker_entry.get("principal_usd")
                or worker_entry.get("target_deployment_usd")
                or worker_entry.get("principal_usd_per_symbol")
            )
            if val is None:
                continue
            out[f"{fund_id}.{sleeve_id}"] = float(val)
    return out


def fund_router_config() -> dict:
    """
    Shape expected by funds/fund_router.py: a dict of fund_id -> {name,
    target_annual_return_pct, max_drawdown_pct, payout_cadence, sleeves:
    {sleeve_id: {target_pct, workers: [<worker_name>]}}}.

    Flattens the policy layout into the router's historical FUND_CONFIG shape
    so the router only cares about "which workers feed which sleeve".
    """
    out = {}
    for fund_id, fund in _load_policy().get("funds", {}).items():
        sleeves = {}
        for sleeve_id, sleeve in fund.get("sleeves", {}).items():
            sleeves[sleeve_id] = {
                "target_pct": sleeve.get("target_pct", 0),
                "workers": list(sleeve.get("workers", {}).keys()),
            }
        out[fund_id] = {
            "name": fund.get("name", fund_id),
            "target_annual_return_pct": fund.get("target_annual_return_pct"),
            "max_drawdown_pct": fund.get("max_drawdown_pct"),
            "payout_cadence": fund.get("payout_cadence"),
            "sleeves": sleeves,
        }
    return out


# ---------- risk (math-engine knobs) ----------


def risk_cfg() -> dict:
    return _load_policy().get("risk", {})


def risk_engine_enabled() -> bool:
    return bool(risk_cfg().get("engine_enabled"))
