# ADR 0001 — One-shot cron workers, no daemons

* **Status**: Accepted
* **Date**: 2026-04-23
* **Context**: Hermes Kite's worker fleet (17 strategy scanners) needs a runtime model.

## Decision

Every worker is a one-shot Python invocation triggered by cron. State lives on disk in `~/.hermes/brain/paper_portfolio.json` (atomic `tmp + rename` writes) and `~/.hermes/brain/status/<worker>.json`. Workers do not maintain in-memory state across cycles. There is no long-lived daemon.

## Considered alternatives

1. **Long-lived service per worker.** A FastAPI/asyncio loop per scanner with internal scheduling. Lower per-cycle overhead. Requires process supervision, health endpoints, graceful-shutdown handling, and explicit state hand-off on restart.
2. **Single supervisor process running all workers.** One Python process with an internal scheduler invoking each worker in turn. Simpler than per-worker services but introduces a SPOF — supervisor crash takes the whole fleet down.
3. **Cloud-native scheduler** (Lambda, Cloud Run jobs, K8s CronJobs). Equivalent semantics to local cron at higher operational complexity. Worth revisiting if/when we leave a single host.

## Why we chose cron

* **Crash-safe by default.** A worker that dies leaves no orphan state; the next cron tick picks up from disk. No watchdog-restart logic needed.
* **State on disk is the only authority.** No in-memory cache to invalidate, no pub/sub to debug. Reading `paper_portfolio.json` tells you exactly what the system thinks.
* **Idempotent operations.** Re-running any worker (or the executor) without state changes is a no-op. This is the contract that makes `scripts/reconcile.py` and the on-chain "nothing to settle" path work.
* **Operational simplicity for a hackathon-scale system.** No supervisor config, no service files, no health endpoints, no graceful-shutdown drama.
* **Easy local reproduction.** `python -m funds.aave_usdc_worker` runs exactly what cron runs.

## Consequences

* Per-cycle cold-start overhead — Python import + `pip install -e .` resolution. Acceptable at minute-scale cron intervals; would be wasteful at sub-second.
* Cross-host coordination is not possible without changes (lockfile or distributed lock). Today it's single-host by design.
* No real-time alerting mid-cycle — `scripts/watchdog.py` runs as its own cron job and detects stale heartbeats after the fact, not during a stuck run. Acceptable for paper trading, would warrant rethinking if running against live capital.
* No observability of a worker's *internal* progress — only the start (cron tick) and the end (status file written). `engine/logging_setup.py`'s structured JSON output mitigates this somewhat.

## When to revisit

Migrate to long-lived services if any of these become true:

- A worker needs to react in <5 seconds to feed updates (e.g., latency-sensitive trading).
- Per-cycle cold-start cost becomes a meaningful fraction of total compute spend.
- We move to multi-host deployment and cron coordination becomes painful.
