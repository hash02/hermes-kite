# Runbook: settlement nonce mismatch

The on-chain wallet nonce diverges from the book's max nonce. Caught by `scripts/reconcile.py`.

## What "nonce" means here

Every Ethereum-family tx from a wallet carries a strictly-increasing `nonce`. Our settlement executor records each broadcast tx in `data/kite_settled.json["txs"]` with its `nonce`. The invariant:

```
on_chain_nonce == max(book.txs[].nonce) + 1
```

If `on_chain > book + 1`: txs were broadcast but not recorded (executor crash mid-flight).
If `on_chain < book + 1`: the book lists txs that were never broadcast (data corruption).

Both are recoverable. Don't panic.

## Reset key rotation case (rare, intentional)

If you just rotated `KITE_PRIVATE_KEY` to a new wallet, the new wallet's nonce starts at 0 while the book may reference the old wallet's nonces. **Don't** use this runbook for that — the right move is to:

1. Append a marker note to `data/agent_registry.json` recording the rotation timestamp and the new wallet address.
2. Treat the new wallet's settlement ledger as a fresh sequence (start a new `kite_settled.json` or partition by wallet).
3. Reconcile against the new wallet only from rotation onwards.

## Recovery: on-chain ahead of book (executor crashed)

The executor sent the tx but didn't `dump_json(SETTLED_FILE, settled)`.

1. Get the nonce range:
   ```bash
   python scripts/reconcile.py --output-dir exports/rc_$(date -u +%Y%m%dT%H%M%SZ)
   # Read the report — note "M tx(s) missing from book"
   ```
2. Pull the wallet history from Kitescan: `https://testnet.kitescan.ai/address/<wallet>` → tx list. For each tx in the missing nonce range:
   - Decode the tx `data` field (hex). It should start with `hermes-kite:<agent_id>:<sleeve>:<sha256>` (or `kite-passport:` for the registration tx).
   - Note the sleeve and the content hash.
3. Append each missing tx to `data/kite_settled.json["txs"]`:
   ```json
   {
     "nonce": <N>,
     "sleeve": "<sleeve>",
     "tx": "<hash without 0x prefix>",
     "content_hash": "<sha256>"
   }
   ```
4. Update `data/kite_settled.json["hashes"][<sleeve>]` to the latest content hash for each affected sleeve.
5. Re-run `python scripts/reconcile.py`. Should be clean.

## Recovery: book ahead of on-chain (fictional entries)

The book references a `tx` hash that doesn't resolve on Kitescan.

1. The reconcile report lists the offending `nonce` and `tx`.
2. Look up the tx on Kitescan. 404 confirms it's fictional.
3. Remove the entry from `data/kite_settled.json["txs"]`. If it was the latest entry for a sleeve, also revert the `hashes[<sleeve>]` entry to the previous content hash (find it in git history of the file).
4. Re-run reconcile. Should be clean. The executor will re-broadcast on the next cycle since the sleeve hash will now diff again.

## Prevention

The executor's write order matters. Today (in `onchain/kite_executor.py`):

```python
tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
# crash here = on-chain ahead of book
settled["hashes"][name] = h
settled.setdefault("txs", []).append({...})
nonce += 1
# end of for-loop
dump_json(SETTLED_FILE, settled)   # <-- single write at the end
```

If the loop crashes after a `send_raw_transaction` but before `dump_json`, the book misses the tx. Mitigations to consider (future PR):

- **Write the tx-record BEFORE broadcasting**, with a `pending: true` marker; flip to confirmed after the broadcast succeeds. Trades crash-recovery clarity for one extra write per tx.
- **Single-tx-per-cycle mode**: write after each tx, not at the loop end. Each cycle fully commits one nonce.
- **Resume-from-pending check** at executor startup: if the book has a `pending` marker, look up the wallet's nonce, infer whether the tx made it.

These tradeoffs warrant their own ADR before changing the protocol.
