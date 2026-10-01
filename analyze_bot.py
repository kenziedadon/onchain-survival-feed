import json

# Backtest results analysis
with open('/home/kenzodadon/base-trend-bot/backtest_results.json') as f:
    data = json.load(f)

print(f"=== BACKTEST SUMMARY ===")
print(f"Total coins tested: {len(data)}")
print()

active = [d for d in data if d['trades'] > 0]
print(f"Coins with trades: {len(active)}")
print()
for d in sorted(active, key=lambda x: x['net_return_pct'], reverse=True):
    print(f"{d['name']:30s} | ret={d['net_return_pct']:+.2f}% | trades={d['trades']} | WR={d['win_rate']:.0f}% | PF={d['profit_factor']:.2f} | score={d['score']}")

print()
no_trades = [d for d in data if d['trades'] == 0]
print(f"Coins with no trades: {len(no_trades)}")
for d in no_trades[:5]:
    print(f"  {d['name']:30s} | score={d['score']} | liq=${d['liq']:,.0f}")

print()
print("=== GRID RESULTS (latest) ===")
with open('/home/kenzodadon/base-trend-bot/grid_results.json') as f:
    grid = json.load(f)
for g in grid[-3:]:
    print(f"fast={g['fast']} slow={g['slow']} | n={g['n']} | prof={g['prof']} | avg={g['avg']:.2f}% | detail={g['detail']}")
