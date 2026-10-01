#!/usr/bin/env python3
"""Write fresh trending-pool candles to candles_cache.json (one network pass)."""
import json, time, urllib.request
from pathlib import Path
import backtest as BT
from indicators import CANDLE_LIMIT

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}
def get(url, timeout=25, retries=8):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except Exception as e:
            back = 8 + i*4 if "429" in str(e) else 2.0
            if i < retries-1: time.sleep(back); continue
            raise

pools = BT.discover_trending(30)
print(f"Discovered {len(pools)} pools")
out = []
for p in pools:
    if p["liquidity"] < 300_000 or p.get("score",0) < 35: continue
    addr = p["address"]
    url = (f"https://api.geckoterminal.com/api/v2/networks/base/pools/"
           f"{addr}/ohlcv/hour?aggregate=1&limit={CANDLE_LIMIT}")
    try:
        rows = json.loads(get(url)).get("data",{}).get("attributes",{}).get("ohlcv_list",[])
    except Exception as e:
        print(f"  [skip {p['name']}] {e}"); continue
    c = [{"ts":r[0],"o":r[1],"h":r[2],"l":r[3],"c":r[4],"v":r[5]} for r in rows]
    if len(c) < 60:
        print(f"  [skip {p['name']}] only {len(c)} candles"); continue
    out.append({"name":p["name"],"address":addr,"liquidity":p["liquidity"],
                "fee_bps":p["fee_bps"],"score":p.get("score",50),"candles":c})
    print(f"  cached {p['name'][:24]:24} {len(c)} candles")
    time.sleep(1.0)
Path("candles_cache.json").write_text(json.dumps(out, indent=2))
print(f"\nWrote {len(out)} coins to candles_cache.json")
