"""Shared logging setup — structured JSON output with cron-run-id correlation.

Every worker should call::

    from engine.logging_setup import setup_logger
    log = setup_logger(__name__)

instead of `logging.basicConfig(...)`. This way, all 17 workers emit one
JSON line per log record, every line is tagged with the same `run_id` for
the cron tick (so log aggregation can stitch them together), and log level
+ output stream are controlled by env vars instead of hardcoded constants.

Environment:
  HERMES_LOG_LEVEL    DEBUG / INFO (default) / WARNING / ERROR
  HERMES_RUN_ID       passed in by the cron wrapper; auto-generated if absent
  HERMES_LOG_STREAM   "stdout" (default) or "stderr"
  HERMES_LOG_FORMAT   "json" (default) or "text"  — text useful for tty dev
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import UTC, datetime

# Once configured for a given worker name we don't reconfigure on
# repeat calls — keeps repeated `setup_logger(__name__)` idempotent.
_CONFIGURED: set[str] = set()
_RUN_ID: str | None = None


def run_id() -> str:
    """Return the cron-run correlation id for the current process.

    Resolution order: HERMES_RUN_ID env var, else a generated UUID4 hex
    string (cached for the process lifetime so all loggers share it).
    """
    global _RUN_ID
    if _RUN_ID is not None:
        return _RUN_ID
    env = os.environ.get("HERMES_RUN_ID")
    _RUN_ID = env if env else uuid.uuid4().hex[:12]
    return _RUN_ID


class _JsonFormatter(logging.Formatter):
    """Emit one JSON line per log record. Includes run_id, worker name,
    severity, message, exception traceback if present."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "run_id": run_id(),
            "msg": record.getMessage(),
        }
        # Common Python idioms — pass extra={} or use exc_info=True
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["exc"] = self.formatException(record.exc_info)
        # Anything attached via `log.info("msg", extra={"k": v})` lands as
        # a top-level attribute of the record. Pull through known-safe ones.
        for k in ("worker", "fund", "sleeve", "category", "sym", "tx"):
            if hasattr(record, k):
                payload[k] = getattr(record, k)
        return json.dumps(payload, default=str)


class _TextFormatter(logging.Formatter):
    """Human-readable single-line format for tty dev work."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s [%(levelname)s] %(name)s [run_id=%(run_id)s]: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )

    def format(self, record: logging.LogRecord) -> str:
        record.run_id = run_id()
        return super().format(record)


def _make_handler() -> logging.Handler:
    stream_name = (os.environ.get("HERMES_LOG_STREAM") or "stdout").lower()
    stream = sys.stderr if stream_name == "stderr" else sys.stdout
    handler = logging.StreamHandler(stream)
    fmt_name = (os.environ.get("HERMES_LOG_FORMAT") or "json").lower()
    handler.setFormatter(_TextFormatter() if fmt_name == "text" else _JsonFormatter())
    return handler


def setup_logger(name: str, level: str | None = None) -> logging.Logger:
    """Get a configured logger for the given module name.

    Idempotent — calling twice with the same name doesn't double-attach
    handlers. Reads HERMES_LOG_LEVEL on first configure for the name.
    """
    logger = logging.getLogger(name)
    if name in _CONFIGURED:
        return logger
    lvl = level or os.environ.get("HERMES_LOG_LEVEL") or "INFO"
    logger.setLevel(lvl.upper())
    # Attach a single handler; clear any pre-existing ones from
    # logging.basicConfig if it was called earlier in this process.
    logger.handlers.clear()
    logger.addHandler(_make_handler())
    logger.propagate = False
    _CONFIGURED.add(name)
    return logger


def reset_for_tests() -> None:
    """Wipe the configured-set + run id cache. Tests use this to get a
    clean state between cases."""
    global _RUN_ID
    _CONFIGURED.clear()
    _RUN_ID = None
