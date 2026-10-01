"""Backtest the exact 12 coins from the latest discovered.jsonl through v8 engine."""
import json, time
from backtest import fetch_ohlcv, backtest, CANDLE_LIMIT

pools = [json.loads(l) for l in open('discovered.jsonl')][-1]['coins']
print(f"=== v8 BACKTEST — {len(pools)} discovered Base pools ===\n")
res, skipped = [], []
for p in pools:
    nm = p['name'][:26]
    if p['liquidity'] < 300_000:
        skipped.append((nm, f"liq ${p['liquidity']:,.0f} < 300k")); continue
    if p.get('score', 0) < 35:
        skipped.append((nm, f"score {p.get('score')} < 35")); continue
    try:
        c = fetch_ohlcv(p['address'], 'hour', CANDLE_LIMIT); time.sleep(2.0)
    except Exception as e:
        skipped.append((nm, f"OHLCV error: {e}")); continue
    if len(c) < 60:
        skipped.append((nm, f"only {len(c)} candles (<60)")); continue
    r = backtest(c, p['fee_bps'], p['liquidity'], score=p.get('score', 50))
    if not r:
        skipped.append((nm, "engine returned no result")); continue
    r.update(name=p['name'], liq=p['liquidity'], score=p.get('score', 50), addr=p['address'])
    res.append(r)
    print(f"  {nm:26} {'✅ PROFIT' if r['net_return_pct']>0 else '❌ LOSS  '} "
          f"net={r['net_return_pct']:+7.1f}% trades={r['trades']:2} "
          f"win={r['win_rate']:4.0f}% PF={r['profit_factor']:.2f} DD={r['max_drawdown_pct']:.1f}%")

print("\n=== SKIPPED (never reached engine) ===")
for n, why in skipped:
    print(f"  {n:26} {why}")

if res:
    wins = [r for r in res if r['net_return_pct'] > 0]
    basket = sum(r['net_return_pct'] for r in res) / len(res)
    print(f"\n=== SUMMARY ===\ntested={len(res)}  profitable={len(wins)} "
          f"({100*len(wins)/len(res):.0f}%)  basket_avg={basket:+.2f}%  "
          f"total_trades={sum(r['trades'] for r in res)}")
    print("\nRanked by net return:")
    for r in sorted(res, key=lambda x: -x['net_return_pct']):
        print(f"  {r['name'][:26]:26} net={r['net_return_pct']:+7.1f}% "
              f"PF={r['profit_factor']:.2f} trades={r['trades']:2} {r['addr']}")
    json.dump(res, open('bt_the12_results.json', 'w'), indent=1)
    print("\nWrote bt_the12_results.json")
else:
    print("\nNo pool reached the engine — nothing backtested.")
