#!/usr/bin/env python3
import json, os, sys
from risk import _live_balance, capital_band, load_state, load_caps
from _tolerant_read import iter_jsonl_entries

print('=== WALLET ===')
bal = _live_balance()
if bal:
    band_min, band_max = capital_band(bal)
    print(f'Balance: {bal:.6f} ETH')
    print(f'Capital band: {band_min:.6f} - {band_max:.6f} ETH')
    print(f'Max trade: {band_max:.6f} ETH')

print('\n=== RISK STATE ===')
state = load_state()
print(f'Day: {state.get("day")}')
print(f'Spent: {state.get("spent_eth", 0):.6f} ETH')
print(f'PnL: {state.get("pnl_eth", 0):.6f} ETH')
print(f'Trades: {len(state.get("trades", []))}')
print(f'Blocked: {len(state.get("blocked", []))}')

print('\n=== CAPS ===')
caps = load_caps()
print(f'live_trading: {caps["live_trading"]}')
print(f'kill_switch: {caps["kill_switch"]}')
print(f'require_confirmation: {caps["require_confirmation"]}')
print(f'max_trade_eth: {caps.get("max_trade_eth")}')
print(f'min_score: {caps.get("min_score")}')

print('\n=== LATEST BACKTEST ===')
try:
    r = json.load(open('backtest_results.json'))
    prof = [x for x in r if x.get('net_return_pct', 0) > 0]
    avg = sum(x.get('net_return_pct', 0) for x in r) / len(r) if r else 0
    basket = 1.0
    for x in r:
        basket *= (0.5 + 0.5 * (1 + x.get('net_return_pct', 0)/100))
    print(f'Coins tested: {len(r)}')
    print(f'Profitable: {len(prof)}/{len(r)} ({len(prof)/len(r)*100:.0f}%)')
    print(f'Avg return: {avg:+.1f}%')
    print(f'Basket return: {(basket-1)*100:+.1f}%')
    if r:
        best = max(r, key=lambda x: x.get('net_return_pct', 0))
        worst = min(r, key=lambda x: x.get('net_return_pct', 0))
        print(f'Best: {best["name"]} {best["net_return_pct"]:+.1f}%')
        print(f'Worst: {worst["name"]} {worst["net_return_pct"]:+.1f}%')
except Exception as e:
    print(f'No backtest results: {e}')

print('\n=== LATEST DISCOVERY ===')
try:
    recs = list(iter_jsonl_entries('discovered.jsonl'))
    if not recs:
        raise FileNotFoundError('discovered.jsonl missing')
    rec = recs[-1]
    coins = rec.get('coins', [])
    print(f'Coins discovered: {len(coins)}')
    for c in coins[:5]:
        print(f'  {c["name"][:26]:26} score={c.get("score",0):>3} liq=${c.get("liquidity",0):>10,.0f}')
except Exception as e:
    print(f'No discovery: {e}')

print('\n=== CRON JOBS ===')
cron_dir = os.path.expanduser('~/.hermes/cron')
if os.path.exists(cron_dir):
    for fname in sorted(os.listdir(cron_dir)):
        if fname.endswith('.json'):
            with open(os.path.join(cron_dir, fname)) as f:
                j = json.load(f)
            name = j.get('name', fname)
            status = j.get('state', '?')
            enabled = j.get('enabled', False)
            last_status = j.get('last_status', 'n/a')
            if 'base-trend' in name or 'base-bot' in name or 'momentum' in name.lower():
                print(f'  {name[:45]:45} {status:12} enabled={enabled} last={last_status}')
