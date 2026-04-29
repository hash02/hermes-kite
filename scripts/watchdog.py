#!/usr/bin/env python3
"""Worker watchdog — alerts on stale or missing status files.

Every worker writes ``~/.hermes/brain/status/<worker>.json`` at the end of
each cron cycle with a fresh ``last_heartbeat`` (ISO-8601). This script
walks that directory and flags:

  * status files older than ``--max-age`` minutes (default 60)
  * malformed status files (unparseable JSON, missing last_heartbeat)
  * configured workers (per ``engine.risk_engine._WORKER_META``) that have
    no status file at all

Mirrors :mod:`scripts.reconcile`'s Report/Finding shape so ops scripts
produce structurally-identical reports for downstream tooling.

Exit code 0 on clean, 1 on any error finding. Suitable for cron + alerting.

Usage::

    python -m scripts.watchdog                          # default 60min threshold
    python -m scripts.watchdog --max-age 30              # tighter
    python -m scripts.watchdog --status-dir /custom/path # custom dir
    python -m scripts.watchdog --json                    # structured output
    python -m scripts.watchdog --output-dir exports/wd   # archive report
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Self-bootstrap when run directly without `pip install -e .`
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from engine.risk_engine import _WORKER_META  # type: ignore
except Exception:  # pragma: no cover
    _WORKER_META = {}

DEFAULT_STATUS_DIR = Path.home() / ".hermes" / "brain" / "status"
DEFAULT_MAX_AGE_MINUTES = 60


@dataclass
class Finding:
    severity: str  # "ok", "warn", "error"
    category: str  # "stale", "malformed", "missing", "fresh"
    message: str
    worker: str = ""


@dataclass
class Report:
    timestamp: str
    status_dir: str
    max_age_minutes: int
    workers_seen: int = 0
    workers_expected: int = 0
    findings: list[Finding] = field(default_factory=list)

    def add(self, severity: str, category: str, message: str, worker: str = "") -> None:
        self.findings.append(Finding(severity, category, message, worker))

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    @property
    def clean(self) -> bool:
        return self.error_count == 0


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _check_status_file(path: Path, threshold: datetime, report: Report) -> None:
    """Validate a single status file's freshness + shape."""
    worker = path.stem
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        report.add("error", "malformed", f"unreadable: {e}", worker=worker)
        return

    heartbeat_raw = data.get("last_heartbeat")
    if not heartbeat_raw:
        report.add("error", "malformed", "missing last_heartbeat", worker=worker)
        return

    heartbeat = _parse_iso(heartbeat_raw)
    if heartbeat is None:
        report.add(
            "error", "malformed", f"unparseable last_heartbeat: {heartbeat_raw!r}", worker=worker
        )
        return

    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=UTC)
    age = threshold - heartbeat
    if heartbeat < threshold:
        # stale
        report.add(
            "error",
            "stale",
            f"last_heartbeat {heartbeat.isoformat()} is older than threshold "
            f"({int(age.total_seconds() / 60)}min)",
            worker=worker,
        )
    else:
        report.add(
            "ok",
            "fresh",
            f"last_heartbeat {heartbeat.isoformat()} ({-int(age.total_seconds() / 60)}min ago)",
            worker=worker,
        )


def run(
    status_dir: Path, max_age_minutes: int, expected_workers: list[str] | None = None
) -> Report:
    now = datetime.now(UTC)
    threshold = now - timedelta(minutes=max_age_minutes)
    expected = list(expected_workers) if expected_workers is not None else list(_WORKER_META.keys())

    report = Report(
        timestamp=now.isoformat(),
        status_dir=str(status_dir),
        max_age_minutes=max_age_minutes,
        workers_expected=len(expected),
    )

    if not status_dir.exists():
        report.add("error", "missing", f"status dir does not exist: {status_dir}")
        return report

    files = sorted(status_dir.glob("*.json"))
    report.workers_seen = len(files)
    seen_workers = {f.stem for f in files}

    for path in files:
        _check_status_file(path, threshold, report)

    # Configured workers that never wrote a status file
    for w in expected:
        if w not in seen_workers:
            report.add("error", "missing", "no status file ever written", worker=w)

    return report


def _print_human(report: Report) -> None:
    print()
    print(f"=== Watchdog report  {report.timestamp} ===")
    print(f"status_dir:        {report.status_dir}")
    print(f"max_age_minutes:   {report.max_age_minutes}")
    print(f"workers_seen:      {report.workers_seen}")
    print(f"workers_expected:  {report.workers_expected}")
    print()
    for f in report.findings:
        marker = {"ok": "  ", "warn": "~ ", "error": "! "}.get(f.severity, "  ")
        worker = f" [{f.worker}]" if f.worker else ""
        print(f"  [{f.severity:<5}] {marker}{f.category:<10}{worker}  {f.message}")
    print()
    if report.clean:
        ok_count = sum(1 for f in report.findings if f.severity == "ok")
        print(f"RESULT: clean ({ok_count} fresh, 0 errors)")
    else:
        print(f"RESULT: ALERT ({report.error_count} error(s))")


def _to_json(report: Report) -> dict:
    return {
        **{k: v for k, v in asdict(report).items() if k != "findings"},
        "findings": [asdict(f) for f in report.findings],
        "clean": report.clean,
        "error_count": report.error_count,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Hermes worker freshness watchdog")
    ap.add_argument(
        "--status-dir",
        type=Path,
        default=DEFAULT_STATUS_DIR,
        help=f"Directory of *.json status files (default: {DEFAULT_STATUS_DIR})",
    )
    ap.add_argument(
        "--max-age",
        type=int,
        default=DEFAULT_MAX_AGE_MINUTES,
        help=f"Stale threshold in minutes (default: {DEFAULT_MAX_AGE_MINUTES})",
    )
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of human text")
    ap.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Archive a watchdog_<ts>.json report here",
    )
    args = ap.parse_args()

    report = run(args.status_dir, args.max_age)

    if args.json:
        print(json.dumps(_to_json(report), indent=2))
    else:
        _print_human(report)

    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        tag = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = args.output_dir / f"watchdog_{tag}.json"
        out.write_text(json.dumps(_to_json(report), indent=2))
        if not args.json:
            print(f"  report written to {out}")

    sys.exit(0 if report.clean else 1)


if __name__ == "__main__":
    main()
