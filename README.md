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

## Proof of life

First 8 settlement marker txs on Kite testnet (chain 2368), nonce 0-7, wallet 0xA29fF03ABfd219e3c76D1C18653297B8201B7748:

| sleeve | tx |
|---|---|
| `fund_60_40_income.structural_grid` | [81cfa0075ab9...](https://testnet.kitescan.ai/tx/0x81cfa0075ab910645120a4ad48e710d64cb18ca4894cceb8d34e7f74daf13364) |
| `fund_60_40_income.cash` | [1a82be1dc568...](https://testnet.kitescan.ai/tx/0x1a82be1dc56851226f49cd4e028cc7beda1e9879acafa2a9d81857960e1230f5) |
| `fund_75_25_balanced.structural_grid` | [adf4cf8c9ea5...](https://testnet.kitescan.ai/tx/0xadf4cf8c9ea520c1621e062430250e93b49f71c0b4727de6c16f251eeebbe081) |
| `fund_75_25_balanced.cash` | [87e12bbed1be...](https://testnet.kitescan.ai/tx/0x87e12bbed1bee64dc6a3cc62229ab3f047e68cacb1e507465d2027ab4ed051d9) |
| `fund_90_10_growth.aggressive_grid` | [28f20b046679...](https://testnet.kitescan.ai/tx/0x28f20b046679ed7b10fff8d4a81707c9c152e74a06dadd1b8d355d424ce2fdd7) |
| `fund_90_10_growth.directional_momentum` | [74c74f17549b...](https://testnet.kitescan.ai/tx/0x74c74f17549b77583b988f3265985f6b13cf4d481b0c5af1fe0e129b5f48255c) |
| `fund_90_10_growth.memecoin_sniper` | [169f69e77034...](https://testnet.kitescan.ai/tx/0x169f69e77034a47d70069c116f3d2761eea295dab381fd92515c14846a96458c) |
| `fund_90_10_growth.xstocks_directional` | [05e930c312d9...](https://testnet.kitescan.ai/tx/0x05e930c312d9c74b6d9d0fde765dcc12950d9d1acfb371410f90b0a4df593aee) |

Each tx carries `hermes-kite:{sleeve}:{sha256}` in tx data. A sleeve that has not changed state since its last settlement is skipped on the next run (idempotent).
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
