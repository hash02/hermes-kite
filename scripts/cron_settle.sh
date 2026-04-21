#!/bin/bash
# grab_and_run() heartbeat — refresh portfolio snapshot from live fund state,
# then fire kite_executor to settle any new sleeve flips on Kite.
set -e

cd $(dirname $0)/..
KITE_DIR=$(pwd)

# 1. Refresh local snapshot from live fund state
/usr/bin/python3 - << 'PY'
import json, glob, os, datetime
files = sorted(glob.glob(os.path.expanduser('~/.hermes/brain/funds/*.json')))
out = {
    'sleeves': {},
    'as_of': datetime.datetime.now(datetime.UTC).replace(microsecond=0, tzinfo=None).isoformat() + 'Z',
    'source': 'wukong:~/.hermes/brain/funds/',
}
for f in files:
    fund = os.path.basename(f).replace('.json','')
    d = json.load(open(f))
    for sname, spos in d.get('sleeves', {}).items():
        out['sleeves'][f'{fund}.{sname}'] = spos
target = os.path.expanduser('~/hermes-kite/data/portfolio_summary.json')
with open(target, 'w') as wf:
    json.dump(out, wf, indent=2)
print(f'snapshot: {len(out["sleeves"])} sleeves (funded={sum(1 for v in out["sleeves"].values() if v.get("funded"))})')
PY

# 2. Fire the settlement executor
export KITE_PRIVATE_KEY=$(/usr/bin/python3 -c 'import json,os; print(json.load(open(os.path.expanduser("~/.hermes-kite/wallet.json")))["private_key"])')
/usr/bin/python3 $KITE_DIR/onchain/kite_executor.py

# 3. Log marker
echo "[$(date -u +%FT%TZ)] cron_settle done" >> $KITE_DIR/logs/kite_executor.log
