#!/usr/bin/env python3
'''
polymarket_btc_updown_worker — paper scanner for BTC binary markets.

Per-sleeve fund coverage. Each sleeve in SLEEVE_TARGETS gets its own pool of
NO positions sized to that sleeve's target deployment, tagged with `fund` so
fund_router attributes them only to the owning fund.

Contract (matches aave_usdc + delta_neutral_funding):
  - Tags positions with worker='polymarket_btc_updown', fund=<fund_id>, sleeve=<sleeve_id>
  - Upserts into ~/.hermes/brain/paper_portfolio.json
  - Emits status at ~/.hermes/brain/status/polymarket_btc_updown.json

Strategy (paper, MVP — edge is real but thin, sizing tiny):
  - Gamma API: active, non-closed BTC markets with future endDate + liquidityNum >= $10k
  - Rank by liquidity descending
  - Open NO position when YES price < 0.10 (buy at ~$1 - yes_price; near-certain payoff)
  - Mark-to-market each cycle: size_usd = principal * (current_no_price / entry_no_price)
  - Resolve when endDate passes (snapshot final no_price, set resolved=True)
'''
import json
import logging
import math
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from policy import sleeve_targets_for, worker_cfg

WORKER_NAME = 'polymarket_btc_updown'
PORTFOLIO_FILE = Path.home() / '.hermes/brain/paper_portfolio.json'
STATUS_FILE = Path.home() / '.hermes/brain/status/polymarket_btc_updown.json'
STATE_FILE = Path.home() / '.hermes/brain/state/polymarket_btc_updown_state.json'
GAMMA_URL = 'https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=500&order=volumeNum&ascending=false'

_cfg = worker_cfg(WORKER_NAME)
MAX_POSITION_USD = _cfg.get("max_position_usd", 40.00)
MIN_LIQUIDITY_USD = _cfg.get("min_liquidity_usd", 10000)
YES_PRICE_CEILING = _cfg.get("yes_price_ceiling", 0.10)

_FALLBACK_TARGETS = {
    'fund_75_25_balanced.directional': 200.00,
    'fund_90_10_growth.latency_arb': 300.00,
}
SLEEVE_TARGETS = sleeve_targets_for(WORKER_NAME) or _FALLBACK_TARGETS

log = logging.getLogger(WORKER_NAME)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')


def fetch_markets():
    req = urllib.request.Request(GAMMA_URL, headers={'User-Agent': 'hermes-kite/1.0'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def rank_candidates():
    now = datetime.now(timezone.utc)
    markets = fetch_markets()
    cands = []
    for m in markets:
        q = (m.get('question') or '').lower()
        if not any(t in q for t in ['bitcoin', 'btc']):
            continue
        if m.get('closed'):
            continue
        end_raw = m.get('endDate') or ''
        try:
            end = datetime.fromisoformat(end_raw.replace('Z', '+00:00'))
        except Exception:
            continue
        if end <= now:
            continue
        liq = float(m.get('liquidityNum') or 0)
        if liq < MIN_LIQUIDITY_USD:
            continue
        prices_raw = m.get('outcomePrices') or '[]'
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        if not prices or len(prices) < 2:
            continue
        try:
            yes_price = float(prices[0])
        except Exception:
            continue
        if yes_price >= YES_PRICE_CEILING or yes_price <= 0:
            continue
        no_price = 1.0 - yes_price
        cands.append({
            'slug': m.get('slug'),
            'question': m.get('question'),
            'end_iso': end.isoformat(),
            'liquidity_usd': liq,
            'yes_price': yes_price,
            'no_price': no_price,
        })
    cands.sort(key=lambda c: -c['liquidity_usd'])
    return cands


def load_json(p, default):
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def save_json_atomic(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f'.tmp.{os.getpid()}')
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(p)


def current_yes_price(slug):
    '''Fetch a single market's current YES price.'''
    url = f'https://gamma-api.polymarket.com/markets?slug={slug}'
    req = urllib.request.Request(url, headers={'User-Agent': 'hermes-kite/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
    except Exception:
        return None
    if not d:
        return None
    m = d[0] if isinstance(d, list) else d
    prices_raw = m.get('outcomePrices') or '[]'
    prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
    if not prices:
        return None
    try:
        return float(prices[0])
    except Exception:
        return None


def positions_for_sleeve(positions, sleeve_id, open_only=True):
    out = []
    for p in positions:
        if not isinstance(p, dict):
            continue
        if p.get('worker') != WORKER_NAME:
            continue
        if p.get('sleeve') != sleeve_id:
            continue
        if open_only and p.get('resolved'):
            continue
        out.append(p)
    return out


def position_id(slug, sleeve_id):
    return f'poly_btc_no_{slug[:32]}_{sleeve_id}'


def fill_sleeve(positions, cands, sleeve_id, target_usd, now_iso):
    """Mark-to-market and resolve existing, then open new positions to hit target.

    Returns (open_positions, opened_count, resolved_count).
    """
    fund_id = sleeve_id.split('.', 1)[0]
    open_pos = positions_for_sleeve(positions, sleeve_id)

    resolved_count = 0
    for pos in open_pos:
        slug = pos.get('market_slug')
        if not slug:
            continue
        cur_yes = current_yes_price(slug)
        if cur_yes is None:
            continue
        cur_no = 1.0 - cur_yes
        entry_no = pos.get('entry_no_price') or (1.0 - (pos.get('entry_price') or 0))
        principal = pos.get('principal_usd') or pos.get('size_usd', 0)
        pos['size_usd'] = round(principal * (cur_no / entry_no), 4) if entry_no > 0 else principal
        pos['pnl_usd'] = round(pos['size_usd'] - principal, 4)
        pos['last_mark_time'] = now_iso
        pos['last_no_price'] = round(cur_no, 4)
        try:
            end = datetime.fromisoformat(pos['end_iso'].replace('Z', '+00:00'))
        except Exception:
            end = None
        if end and datetime.now(timezone.utc) >= end:
            pos['resolved'] = True
            pos['resolve_time'] = now_iso
            pos['exit_no_price'] = round(cur_no, 4)
            pos['correct'] = cur_no > entry_no
            resolved_count += 1
            log.info('resolved [%s] %s  final_no=%.4f  pnl=$%.2f',
                     sleeve_id, pos.get('id'), cur_no, pos['pnl_usd'])

    open_pos = positions_for_sleeve(positions, sleeve_id)
    open_slugs = {p.get('market_slug') for p in open_pos}
    deployed = sum(p.get('size_usd', p.get('principal_usd', 0)) for p in open_pos)
    max_slots = math.ceil(target_usd / MAX_POSITION_USD)

    opened = 0
    for c in cands:
        if c['slug'] in open_slugs:
            continue
        if len(open_pos) >= max_slots:
            break
        if deployed >= target_usd:
            break
        size = min(MAX_POSITION_USD, target_usd - deployed)
        if size < 1.0:
            break
        pos = {
            'id': position_id(c['slug'], sleeve_id),
            'worker': WORKER_NAME,
            'fund': fund_id,
            'sleeve': sleeve_id,
            'market_slug': c['slug'],
            'question': c['question'],
            'symbol': 'BTC',
            'side': 'NO',
            'entry_price': c['yes_price'],
            'entry_no_price': c['no_price'],
            'end_iso': c['end_iso'],
            'principal_usd': size,
            'size_usd': size,
            'pnl_usd': 0.0,
            'resolved': False,
            'entry_time': now_iso,
            'liquidity_usd_at_entry': c['liquidity_usd'],
        }
        positions.append(pos)
        open_pos.append(pos)
        open_slugs.add(c['slug'])
        deployed += size
        opened += 1
        log.info('opened NO [%s] %s yes=%.4f no=%.4f size=$%.2f',
                 sleeve_id, c['slug'], c['yes_price'], c['no_price'], size)

    return open_pos, opened, resolved_count


def run_once():
    portfolio = load_json(PORTFOLIO_FILE, {'positions': []})
    positions = portfolio.get('positions', []) if isinstance(portfolio, dict) else portfolio
    now_iso = datetime.now(timezone.utc).isoformat()

    cands = rank_candidates()

    per_sleeve_open = {}
    total_opened = 0
    total_resolved = 0
    for sleeve_id, target in SLEEVE_TARGETS.items():
        open_pos, opened, resolved = fill_sleeve(positions, cands, sleeve_id, target, now_iso)
        per_sleeve_open[sleeve_id] = open_pos
        total_opened += opened
        total_resolved += resolved

    if isinstance(portfolio, dict):
        portfolio['positions'] = positions
        save_json_atomic(PORTFOLIO_FILE, portfolio)
    else:
        save_json_atomic(PORTFOLIO_FILE, positions)

    open_final = [p for ps in per_sleeve_open.values() for p in ps]
    closed_final = [p for p in positions
                    if isinstance(p, dict)
                    and p.get('worker') == WORKER_NAME
                    and p.get('resolved')]
    deployed = sum((p.get('size_usd') or 0) for p in open_final)
    unrealized = sum((p.get('pnl_usd') or 0) for p in open_final)
    realized = sum((p.get('pnl_usd') or 0) for p in closed_final)

    state = load_json(STATE_FILE, {})
    cycle = int(state.get('cycle_count', 0)) + 1

    status = {
        'worker_name': WORKER_NAME,
        'strategy_type': 'polymarket_binary_fade_longshot',
        'status': 'active' if open_final else 'scanning',
        'last_heartbeat': datetime.now().astimezone().isoformat(),
        'cycle_count': cycle,
        'position_summary': {
            'open_positions': len(open_final),
            'closed_positions': len(closed_final),
            'total_capital_deployed_usd': round(deployed, 2),
            'realized_pnl_usd': round(realized, 2),
            'unrealized_pnl_usd': round(unrealized, 2),
            'by_sleeve': {
                sid: {
                    'open_positions': len(ps),
                    'deployed_usd': round(sum((p.get('size_usd') or 0) for p in ps), 2),
                }
                for sid, ps in per_sleeve_open.items()
            },
        },
        'performance': {
            'pnl_all_time': round(realized + unrealized, 2),
            'trades_last_24h': total_opened,
            'win_rate': (sum(1 for p in closed_final if p.get('correct')) / max(1, len(closed_final))) if closed_final else None,
        },
        'risk': {
            'position_sizing_method': 'per_sleeve_target',
            'sleeve_targets_usd': SLEEVE_TARGETS,
            'max_position_usd': MAX_POSITION_USD,
            'yes_price_ceiling': YES_PRICE_CEILING,
            'min_liquidity_usd': MIN_LIQUIDITY_USD,
        },
        'strategy_config': {
            'source': 'polymarket_gamma_public',
            'rule': 'buy NO on BTC binary markets with YES price < 0.10',
            'fund_sleeves': list(SLEEVE_TARGETS.keys()),
        },
        'errors_last_24h': 0,
        'health_check': 'green',
        'this_cycle': {
            'candidates_considered': len(cands),
            'new_positions_opened': total_opened,
            'positions_resolved': total_resolved,
        },
    }
    save_json_atomic(STATUS_FILE, status)

    state['cycle_count'] = cycle
    state['last_run'] = now_iso
    save_json_atomic(STATE_FILE, state)

    print(f'[{WORKER_NAME}] sleeves={len(per_sleeve_open)} open={len(open_final)} '
          f'deployed=${deployed:.2f} unrealized=${unrealized:.4f} realized=${realized:.4f} '
          f'candidates={len(cands)} opened={total_opened} resolved={total_resolved}')


if __name__ == '__main__':
    run_once()
