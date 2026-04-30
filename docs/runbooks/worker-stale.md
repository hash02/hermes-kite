# Runbook: worker stale

`scripts/watchdog.py` exited non-zero with a `stale` or `missing` finding.

## Read the report

```bash
python scripts/watchdog.py --output-dir exports/wd_$(date -u +%Y%m%dT%H%M%SZ)
```

Each finding includes the `worker` name and either `stale` (heartbeat older than `--max-age` minutes) or `missing` (no status file at all).

## Triage

### `stale` — worker hasn't heartbeated recently

1. **Check the cron host.** Did the cron service run? `systemctl status cron` (or equivalent). Did the run die on a host failure?
2. **Check the worker's last log line.** Structured JSON logs from `engine/logging_setup.py` carry `run_id` for each cycle. Filter your log destination by the worker name and check the most recent cycle. Common causes:
   - **Upstream feed unreachable** — DeFiLlama, Binance, Polymarket etc. went down or rate-limited. The worker logs `WARN`, returns degraded status, but should still emit `last_heartbeat`. If it's not even doing that, the failure is upstream of the heartbeat write.
   - **Network partition** — host can't reach external APIs at all. Watchdog will catch this on every worker simultaneously.
   - **Process crash** — Python exception not caught at the cycle boundary. Search the log for `ERROR` or `Traceback` near the last successful cycle.
   - **Wedged file lock or atomic-write failure** — disk full, FS read-only, permissions changed.
3. **Try a manual run.**
   ```bash
   HERMES_LOG_LEVEL=DEBUG python -m funds.<worker_name>
   ```
   Reproduces in foreground; captures the failure live.
4. **If transient and self-resolving** (single API blip): no action — next cron tick will refresh the heartbeat. Confirm with another `python scripts/watchdog.py` after the tick.
5. **If persistent**: open an issue for the worker. Patch the gating, redeploy, observe.

### `missing` — no status file ever written

The worker is configured (in `engine.risk_engine._WORKER_META`) but `~/.hermes/brain/status/<worker>.json` doesn't exist.

1. **Has it ever run?** New workers don't get a status file until the first cycle.
2. **Is it scheduled in cron?** `crontab -l` should list every active worker.
3. **Does the module import cleanly?**
   ```bash
   python -c "import funds.<worker_name>"
   ```
   ImportError? Probably a stale import after a refactor. Run `pip install -e ".[dev]"` to refresh the editable install.
4. **Did the cron user have permission to write to `~/.hermes/brain/status/`?** Check the dir owner; create it if missing (`mkdir -p ~/.hermes/brain/status`).

### `malformed` — status file unreadable

1. **Inspect** the file directly: `cat ~/.hermes/brain/status/<worker>.json`.
2. **Likely cause**: write was interrupted mid-flight. Workers use atomic `tmp + rename` for the *portfolio* file but not for the *status* file (it's allowed to be transiently malformed).
3. **Fix**: just let the next cron tick overwrite it. If watchdog still flags after one cycle, the writer code is broken — open an issue.

## Tightening or relaxing the threshold

`scripts/watchdog.py --max-age 30` — alert sooner. `--max-age 120` — be lenient with hourly cron jobs. Choose based on the cron schedule. Rule of thumb: 2× the worker's cron interval.

## Escalation

If multiple unrelated workers go stale within minutes of each other, the issue is probably host-level (network, disk, cron) rather than worker-level. Check host metrics first.
