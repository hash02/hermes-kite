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
