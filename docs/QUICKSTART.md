# Quickstart — Hermes on Kite

## 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Fund a Kite testnet wallet

Create a wallet (Metamask, Rabby, or `python3 -c "from eth_account import Account; a = Account.create(); print(a.address, a.key.hex())"`) and fund it:

- Faucet: https://faucet.gokite.ai/

## 3. Set environment

```bash
export KITE_PRIVATE_KEY=0x...your-key...
# optional (defaults shown)
export KITE_RPC=https://rpc-testnet.gokite.ai/
export KITE_CHAIN_ID=2368
```

## 4. Run one scanner (paper)

```bash
mkdir -p ~/.hermes/brain/status
python3 funds/aave_usdc_worker.py
# -> writes ~/.hermes/brain/paper_portfolio.json with a position
# -> writes ~/.hermes/brain/status/aave_usdc.json with a status snapshot
```

## 5. Route positions into fund profiles

```bash
python3 funds/fund_router.py
# -> writes ~/.hermes/brain/funds/fund_60_40_income.json (and 75/25, 90/10)
# -> flips sleeves from unfunded -> funded
```

## 6. Settle the sleeve flip on Kite

Copy the portfolio summary into the repo's data/ dir so kite_executor sees it:

```bash
python3 -c "
import json, pathlib
src = pathlib.Path.home() / '.hermes' / 'brain'
funds = {}
for f in (src / 'funds').glob('*.json'):
    funds[f.stem] = json.loads(f.read_text())
out = {'sleeves': {}}
for fname, fdata in funds.items():
    for sleeve_name, sleeve in fdata.get('sleeves', {}).items():
        out['sleeves'][f'{fname}.{sleeve_name}'] = sleeve
pathlib.Path('data/portfolio_summary.json').write_text(json.dumps(out, indent=2))
print('wrote data/portfolio_summary.json')
"

python3 onchain/kite_executor.py
# -> sends one marker tx per newly-funded sleeve to Kite testnet
# -> tx hashes saved to data/kite_settled.json
```

View the txs on https://testnet.kitescan.ai/

## 7. Run on cron (optional)

```cron
15 * * * * cd /path/to/hermes-kite && /path/to/python funds/aave_usdc_worker.py >> logs/aave_usdc.log 2>&1
17 * * * * cd /path/to/hermes-kite && /path/to/python funds/delta_neutral_worker.py >> logs/delta_neutral.log 2>&1
19 * * * * cd /path/to/hermes-kite && /path/to/python funds/polymarket_btc_updown_worker.py >> logs/polymarket.log 2>&1
21 * * * * cd /path/to/hermes-kite && /path/to/python funds/xstocks_grid_worker.py >> logs/xstocks_grid.log 2>&1
23 * * * * cd /path/to/hermes-kite && /path/to/python funds/tv_momentum_worker.py >> logs/tv_momentum.log 2>&1
25 * * * * cd /path/to/hermes-kite && /path/to/python funds/xstocks_directional_worker.py >> logs/xstocks_directional.log 2>&1
45 * * * * cd /path/to/hermes-kite && /path/to/python funds/fund_router.py >> logs/fund_router.log 2>&1
55 * * * * cd /path/to/hermes-kite && /path/to/python onchain/kite_executor.py >> logs/kite_executor.log 2>&1
```

Offsets stop workers from fighting for the same second. Kite executor runs last so it settles whatever the router flipped that hour.
