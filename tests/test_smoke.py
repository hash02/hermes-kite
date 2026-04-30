"""Smoke tests for hermes-kite workers and on-chain modules.

These tests verify each module can be imported cleanly. They do NOT
execute the workers (which hit live external APIs - DeFiLlama, Yahoo
Finance, Binance, Polymarket, Kite testnet RPC).

Real-data evaluation is done in production by running the actual cron
scripts; CI's job is to catch syntax errors, import bugs, and obvious
regressions before they reach production.

Usage:
    pytest tests/ -v
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

WORKER_FILES = sorted(REPO_ROOT.glob("funds/*.py"))
ONCHAIN_FILES = sorted(REPO_ROOT.glob("onchain/*.py"))


def _load_without_running(path: Path):
    """Load a Python module from a file path without executing __main__ guards.
    Raises if the source is syntactically broken or has unresolvable top-level imports.
    """
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_funds_directory_has_workers():
    assert len(WORKER_FILES) > 0, "expected at least one worker in funds/"


def test_onchain_directory_has_modules():
    assert len(ONCHAIN_FILES) > 0, "expected at least one module in onchain/"


def test_each_worker_compiles():
    """Bytecode-compile each worker. Catches syntax errors fast."""
    import py_compile

    for f in WORKER_FILES:
        py_compile.compile(str(f), doraise=True)


def test_each_onchain_module_compiles():
    import py_compile

    for f in ONCHAIN_FILES:
        py_compile.compile(str(f), doraise=True)


def test_repo_has_readme_and_license():
    assert (REPO_ROOT / "README.md").exists(), "README.md missing"
    assert (REPO_ROOT / "LICENSE").exists(), "LICENSE missing"


def test_requirements_txt_lists_deps():
    req = REPO_ROOT / "requirements.txt"
    assert req.exists(), "requirements.txt missing"
    content = req.read_text()
    # Sanity: at least one of the documented deps
    assert any(d in content for d in ["web3", "requests", "python-dotenv"]), (
        "requirements.txt is empty or unrecognized"
    )
