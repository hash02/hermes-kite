# Hermes on Kite

An autonomous portfolio manager. Scanners `grab()` the signal, `run()` the trade, write state, sleep. Wake on cron and do it again.

Submitted to the **Kite AI Global Hackathon 2026** (Agentic Trading & Portfolio Management track).

## What it does

Six scanner workers cover three model portfolios (60/40 income, 75/25 balanced, 90/10 growth). Each worker owns one job: pull a live feed, decide position size, write state to a shared portfolio ledger, emit a status snapshot. A fund router reads the ledger and flips sleeves to `funded` when their assigned worker lands a position.

All paper. Real market data. One on-chain executor sends settlement to Kite testnet for verifiable on-chain record.

## Architecture — `grab_and_run()` loop

```
           ┌─────────────────────────────────────────┐
           │          grab_and_run() loop            │
           │                                         │
  cron ──▶ │   scanner ──▶ grab(feed) ──▶ decide     │
           │                  │              │       │
           │                  ▼              ▼       │
           │            portfolio.json ──▶ status    │
           │                  │                      │
           │                  ▼                      │
           │        fund_router ──▶ flip sleeve      │
           │                  │                      │
           │                  ▼                      │
           │        kite_executor ──▶ on-chain tx    │
           └─────────────────────────────────────────┘
```

No daemons. No long-lived processes. Every worker is a cron job that runs once, writes state, exits.

## Scanners shipping in this repo

| Worker | Feed | Sleeve |
|---|---|---|
| `aave_usdc_worker.py` | DeFiLlama Aave V3 Ethereum USDC supply APY | stablecoin_yield |
| `delta_neutral_worker.py` | Binance perp funding + spot | delta_neutral |
| `polymarket_btc_updown_worker.py` | Polymarket Gamma API BTC daily up/down | event_edge |
| `xstocks_grid_worker.py` | xStocks tokenized equity price | equity_grid |
| `xstocks_directional_worker.py` | xStocks + volume | equity_directional |
| `tv_momentum_worker.py` | TradingView RSS momentum scan | momentum |

## Kite integration

`onchain/kite_executor.py` signs and broadcasts settlement transactions to Kite testnet:

- **Chain ID:** 2368
- **RPC:** https://rpc-testnet.gokite.ai/
- **Explorer:** https://testnet.kitescan.ai/
- **Faucet:** https://faucet.gokite.ai/

Each time `fund_router.py` flips a sleeve from `unfunded → funded`, `kite_executor.py` writes an on-chain marker tx carrying the sleeve name and position hash. That gives every paper position a verifiable timestamp on Kite.

## Agent Passport

Hermes registers itself as a DID-style agent identity on Kite before settling anything:

```
agent_id:    hermes-kite-portfolio-manager
version:     v0.1.0
controller:  0xA29fF03ABfd219e3c76D1C18653297B8201B7748
chain_id:    2368
protocol:    did:kite-testnet:self-attested
passport tx: https://testnet.kitescan.ai/tx/0x00c569ea55cf73354e408f5e23733e8e17cd12768122e9c3e2f11a1139a1c9f8
```

Capabilities (signed into the passport payload):

```json
["scan:market-feeds", "decide:sleeve-allocation", "route:fund-sleeves", "settle:onchain-markers"]
```

Every settlement marker after registration prefixes the tx data with `hermes-kite:AGENT_ID:SLEEVE:SHA256`. Judges can trace any on-chain action back to the attested agent identity. One DID, signed capabilities, verifiable on-chain.

Self-attested for the hackathon scope. Upgrades to the full Kite Passport CLI + DID registry when that flow is public.

## Proof of life

Settlement marker txs on Kite testnet (chain 2368), wallet 0xA29fF03ABfd219e3c76D1C18653297B8201B7748. Each tx data decodes to `hermes-kite:AGENT_ID:SLEEVE:SHA256`:

| # | sleeve | tx |
|---|---|---|
| 1 | `fund_60_40_income.structural_grid` | [1d668d3ff7...](https://testnet.kitescan.ai/tx/0x1d668d3ff732f6a4851169e19994e551d1b38fa991ef1f8b1219ec45799bb7a2) |
| 2 | `fund_60_40_income.cash` | [2f99e305ca...](https://testnet.kitescan.ai/tx/0x2f99e305cac115802df50f828fd6ed900a02de2d308a3f26a01d22693099b8ef) |
| 3 | `fund_75_25_balanced.structural_grid` | [eaa2c7b389...](https://testnet.kitescan.ai/tx/0xeaa2c7b38974d679f31c5bb3a49995eaad8fbcaabdfef5a8af6d65feb640057e) |
| 4 | `fund_75_25_balanced.cash` | [203de44b89...](https://testnet.kitescan.ai/tx/0x203de44b89b004133ca659abf80b7e58f97333b5119c2a51fe1f5d0aca0767aa) |
| 5 | `fund_90_10_growth.aggressive_grid` | [f0d7ccb2cb...](https://testnet.kitescan.ai/tx/0xf0d7ccb2cb146eed5616fafc71452e4b96535a3ea743695cc32e7dfbc71aca9e) |
| 6 | `fund_90_10_growth.directional_momentum` | [b93bb98c1a...](https://testnet.kitescan.ai/tx/0xb93bb98c1ab3b49b25cdb63b2d828d4d85c0b2e1e998355456a4facb23df861e) |
| 7 | `fund_90_10_growth.memecoin_sniper` | [6f3f0b32ca...](https://testnet.kitescan.ai/tx/0x6f3f0b32ca0ca9308ea8755edd221057468ab89722a787f42a77186eba222cc9) |
| 8 | `fund_90_10_growth.xstocks_directional` | [70c89b6712...](https://testnet.kitescan.ai/tx/0x70c89b6712461a28e4651bfec65d7c795b9c33bc849e069c493bbf5e66ff7851) |

Re-running the heartbeat without new sleeve flips prints `nothing to settle`. Idempotent by design.

## Live demo

Public dashboard: https://bionicbanker.tech/portfolio

## Run locally

```bash
pip install -r requirements.txt
export KITE_PRIVATE_KEY=0x...   # funded via https://faucet.gokite.ai/
python3 funds/aave_usdc_worker.py
python3 onchain/kite_executor.py  # settle latest portfolio delta on Kite
```

## License

MIT
