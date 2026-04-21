#!/usr/bin/env python3
"""
Agent Passport registration — writes a DID-style self-attested agent identity
to Kite testnet.

Kite's differentiating primitive is the Agent Passport: every AI agent carries
a cryptographically verifiable DID bound to its controlling wallet. Hermes
registers itself once, and every subsequent settlement tx prefixes its data
with the registered agent_id + passport hash. Judges can trace any on-chain
action back to the attested identity.

This is a lightweight DID-style attestation (not the full Kite Passport CLI
flow); it demonstrates the pattern in a hackathon-friendly way.

Run once. Re-runs are idempotent (skip if passport already on file).
"""
from __future__ import annotations
import hashlib
import json
import os
import datetime
from pathlib import Path
from web3 import Web3

KITE_RPC = os.environ.get('KITE_RPC', 'https://rpc-testnet.gokite.ai/')
KITE_CHAIN_ID = int(os.environ.get('KITE_CHAIN_ID', '2368'))
PRIV_KEY = os.environ.get('KITE_PRIVATE_KEY')

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_FILE = ROOT / 'data' / 'agent_registry.json'

AGENT_ID = 'hermes-kite-portfolio-manager'
AGENT_VERSION = 'v0.1.0'
AGENT_CAPABILITIES = [
    'scan:market-feeds',
    'decide:sleeve-allocation',
    'route:fund-sleeves',
    'settle:onchain-markers',
]


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def main() -> int:
    if not PRIV_KEY:
        raise SystemExit('KITE_PRIVATE_KEY not set')

    w3 = Web3(Web3.HTTPProvider(KITE_RPC))
    acct = w3.eth.account.from_key(PRIV_KEY)

    if REGISTRY_FILE.exists():
        reg = json.loads(REGISTRY_FILE.read_text())
        if reg.get('agent_id') == AGENT_ID and reg.get('version') == AGENT_VERSION:
            print(f"[passport] already registered -> agent_id={AGENT_ID} tx={reg.get('tx')}")
            return 0

    # Build passport payload (DID-style self-attestation)
    payload = {
        'agent_id': AGENT_ID,
        'version': AGENT_VERSION,
        'controller': acct.address,
        'capabilities': AGENT_CAPABILITIES,
        'as_of': datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
        'chain_id': KITE_CHAIN_ID,
        'protocol': 'did:kite-testnet:self-attested',
    }
    payload_json = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    payload_hash = sha256(payload_json)

    # Tx data format: kite-passport:{agent_id}:{payload_hash}
    marker = f'kite-passport:{AGENT_ID}:{payload_hash}'
    data_hex = '0x' + marker.encode().hex()

    nonce = w3.eth.get_transaction_count(acct.address)
    gas_price = w3.eth.gas_price
    tx = {
        'to': acct.address,
        'value': 0,
        'gas': 40000,
        'gasPrice': gas_price,
        'nonce': nonce,
        'chainId': KITE_CHAIN_ID,
        'data': data_hex,
    }
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hex = tx_hash.hex()

    # Write registry
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps({
        'agent_id': AGENT_ID,
        'version': AGENT_VERSION,
        'controller': acct.address,
        'payload': payload,
        'payload_hash': payload_hash,
        'tx': tx_hex,
        'chain_id': KITE_CHAIN_ID,
        'explorer': f'https://testnet.kitescan.ai/tx/0x{tx_hex}',
    }, indent=2, sort_keys=True))

    print(f'[passport] registered agent_id={AGENT_ID}')
    print(f'[passport] payload_hash={payload_hash}')
    print(f'[passport] tx=0x{tx_hex}')
    print(f'[passport] explorer=https://testnet.kitescan.ai/tx/0x{tx_hex}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
