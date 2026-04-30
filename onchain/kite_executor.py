#!/usr/bin/env python3
"""
Kite executor — signs and broadcasts a settlement marker tx to Kite testnet.

Reads the latest sleeve flip from data/portfolio_summary.json. For any sleeve
that went unfunded -> funded since the last settlement, write a tiny marker tx
on Kite carrying the sleeve name + position hash in tx data.

Why a marker tx instead of a full on-chain position? This is a hackathon port
of a paper-trading portfolio manager. Every sleeve flip gets a verifiable
timestamp + content-hash on Kite without moving real capital. The same pattern
extends to real on-chain execution by swapping the marker for a real swap /
supply call on Kite-deployed DeFi contracts.

grab() the pending settlement. run() the tx. Write state. Sleep.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from web3 import Web3

KITE_RPC = os.environ.get("KITE_RPC", "https://rpc-testnet.gokite.ai/")
KITE_CHAIN_ID = int(os.environ.get("KITE_CHAIN_ID", "2368"))
PRIV_KEY = os.environ.get("KITE_PRIVATE_KEY")

ROOT = Path(__file__).resolve().parent.parent
PORTFOLIO_FILE = ROOT / "data" / "portfolio_summary.json"
SETTLED_FILE = ROOT / "data" / "kite_settled.json"
REGISTRY_FILE = ROOT / "data" / "agent_registry.json"


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def dump_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True))


def sleeve_hash(sleeve_name: str, position: dict) -> str:
    """Deterministic content hash for a sleeve position."""
    payload = json.dumps(
        {"sleeve": sleeve_name, "pos": position},
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def main():
    if not PRIV_KEY:
        print("ERROR: set KITE_PRIVATE_KEY env var (fund via https://faucet.gokite.ai/)", file=sys.stderr)
        sys.exit(1)

    w3 = Web3(Web3.HTTPProvider(KITE_RPC))
    if not w3.is_connected():
        print(f"ERROR: cannot reach Kite RPC {KITE_RPC}", file=sys.stderr)
        sys.exit(2)

    acct = w3.eth.account.from_key(PRIV_KEY)
    print(f"[kite] wallet {acct.address}")
    print(f"[kite] chain id {w3.eth.chain_id} (expected {KITE_CHAIN_ID})")
    balance = w3.eth.get_balance(acct.address)
    print(f"[kite] balance {w3.from_wei(balance, 'ether')} KITE")

    portfolio = load_json(PORTFOLIO_FILE, {})
    sleeves = portfolio.get("sleeves", {})
    settled = load_json(SETTLED_FILE, {"hashes": {}})
    registry = load_json(REGISTRY_FILE, {})
    agent_id = registry.get("agent_id", "unregistered")
    passport_hash = registry.get("payload_hash", "")
    if agent_id != "unregistered":
        print(f"[kite] agent_id={agent_id} passport={passport_hash[:12]}...")

    pending = []
    for name, pos in sleeves.items():
        if not pos.get("funded"):
            continue
        h = sleeve_hash(name, pos)
        if settled["hashes"].get(name) == h:
            continue  # already settled at this state
        pending.append((name, pos, h))

    if not pending:
        print("[kite] nothing to settle -- portfolio unchanged since last run")
        return

    nonce = w3.eth.get_transaction_count(acct.address)
    gas_price = w3.eth.gas_price

    for name, _pos, h in pending:
        data = f"hermes-kite:{agent_id}:{name}:{h}".encode().hex()
        tx = {
            "to": acct.address,  # self-send marker
            "value": 0,
            "gas": 30000,
            "gasPrice": gas_price,
            "nonce": nonce,
            "chainId": KITE_CHAIN_ID,
            "data": "0x" + data,
        }
        signed = acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"[kite] settled sleeve={name}  tx={tx_hash.hex()}")
        settled["hashes"][name] = h
        settled.setdefault("txs", []).append(
            {
                "sleeve": name,
                "tx": tx_hash.hex(),
                "content_hash": h,
                "nonce": nonce,
            }
        )
        nonce += 1

    dump_json(SETTLED_FILE, settled)
    print(f"[kite] settled {len(pending)} sleeve(s)")


if __name__ == "__main__":
    main()
