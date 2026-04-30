# Runbook: reconcile failure

`scripts/reconcile.py` exited non-zero. Triage by category.

## Read the report first

```bash
python scripts/reconcile.py --output-dir exports/rc_$(date -u +%Y%m%dT%H%M%SZ)
```

The report lists findings as `[severity] category  message`. Errors block clean. Warnings don't fail the script but tell you something is off.

## By category

### `book` — book-side integrity

| Message | What it means | Fix |
|---|---|---|
| `duplicate nonces in book: [...]` | `data/kite_settled.json` has two entries with the same `nonce`. | The executor double-recorded. Inspect both txs on Kitescan; keep the one that's actually on chain, delete the other from the JSON. Re-run reconcile. |
| `non-contiguous nonce range: gap(s) [(a,b)]` | Gap between consecutive `nonce` values. | Often benign — the wallet sent a non-Hermes tx between settlements. Confirm the missing nonce on Kitescan from the wallet. If it's also a Hermes tx that didn't get logged, append it manually and re-run. |
| `N tx(s) have malformed content_hash` | A `content_hash` field doesn't match `^[0-9a-fA-F]{64}$`. | Probable hand-edit of `kite_settled.json`. Recompute via `python -c "import hashlib, json; ..."` for the affected sleeve or restore from git history. |
| `N tx(s) have malformed tx hash` | The `tx` field isn't a 64-hex string. | Same fix as above. |
| `N sleeve(s) in hashes/ with no matching tx` | `kite_settled.json["hashes"][sleeve]` references a content hash that no `txs[]` entry holds. | Either re-broadcast (the executor will pick it up next cycle) or remove the orphan entry from `hashes`. |

### `passport` — agent passport integrity

| Message | What it means | Fix |
|---|---|---|
| `passport hash drift: stored=X computed=Y` | `data/agent_registry.json["payload"]` was edited after `payload_hash` was committed. | This is a tamper signal. Check git log for unauthorized edits. If legit (e.g. version bump), re-register the passport via `python onchain/register_agent.py`. |
| `registry has no stored payload_hash` | Agent never registered, or hash was stripped. | Run `python onchain/register_agent.py` (needs `KITE_PRIVATE_KEY`). |

### `nonce` — on-chain nonce alignment

| Message | What it means | Fix |
|---|---|---|
| `on-chain nonce N > book max+1 — M tx(s) missing from book` | Wallet sent txs that aren't recorded. Probably the executor crashed between `send_raw_transaction` and the JSON write. | Pull recent wallet history from Kitescan, identify the unaccounted txs (decode their `data` field — Hermes markers start with `hermes-kite:`), append them to `data/kite_settled.json`. |
| `on-chain nonce N < book max+1 — book claims txs that aren't on-chain` | Book has fictional entries — never broadcast. | Identify the offending entries by looking up their `tx` on Kitescan (404 = fictional). Remove them from `kite_settled.json`. Run reconcile. |

### `tx` — per-tx existence

| Message | What it means | Fix |
|---|---|---|
| `tx 0x... (nonce N) not found on Kite: ...` | Specific tx hash doesn't resolve. | Same as "fictional tx" above for that specific entry. |
| `tx 0x... not from expected wallet` | Tx exists but `from` address mismatches. | Don't blindly accept — investigate. The wallet could have been compromised, or the executor was run with a different key. |

## Rerun discipline

After any fix:

```bash
python scripts/reconcile.py --skip-onchain     # book integrity is clean
python scripts/reconcile.py                    # full check passes
```

Both must exit 0 before declaring "resolved". Add the report path to your incident notes.

## Escalation

If reconcile fails for reasons not covered here, file a security advisory (see `SECURITY.md`) — especially anything that looks like wallet compromise or hash drift you can't explain.
