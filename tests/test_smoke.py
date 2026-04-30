"""Smoke tests verifying repo structure and import-time syntax.

Complements the existing test_backtest, test_nav_accounting,
test_reconcile, test_risk_engine suites (69 tests) by adding fast
structural assertions that catch repo-shape regressions.

Usage:
    pytest tests/test_smoke.py -v
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_funds_directory_has_workers():
    workers = list(REPO_ROOT.glob("funds/*.py"))
    assert len(workers) > 0, "expected at least one worker in funds/"


def test_onchain_directory_has_modules():
    onchain = list(REPO_ROOT.glob("onchain/*.py"))
    assert len(onchain) > 0


def test_each_worker_compiles():
    import py_compile

    for f in REPO_ROOT.glob("funds/*.py"):
        py_compile.compile(str(f), doraise=True)


def test_each_onchain_module_compiles():
    import py_compile

    for f in REPO_ROOT.glob("onchain/*.py"):
        py_compile.compile(str(f), doraise=True)


def test_repo_has_readme_and_license():
    assert (REPO_ROOT / "README.md").exists()
    assert (REPO_ROOT / "LICENSE").exists()


def test_requirements_txt_lists_deps():
    req = REPO_ROOT / "requirements.txt"
    assert req.exists()
    text = req.read_text()
    assert any(d in text for d in ["web3", "requests", "python-dotenv"])
