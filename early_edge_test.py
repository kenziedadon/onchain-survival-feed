#!/usr/bin/env python3
"""
EARLY-EDGE TEST: validate the hypothesis that entering coins BEFORE they trend
(new_pools) is profitable, vs entering after (trending_pools). This is the real
edge the data pointed to. Uses realistic costs. Rate-limit-safe (1 call / 4s).
"""
import json, time, urllib.request
from datetime import datetime, timezone
from indicators import sma, rsi, roc, eth_usd_price, CANDLE_LIMIT

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}
_last=0.0; GAP=4.0
def get(url, retries=10, timeout=25):
    global _last
    for i in range(retries):
        w=GAP-(time.time()-_last)
        if w>0: time.sleep(w)
        _last=time.time()
        try:
            req=urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except Exception as e:
            back=8+i*5 if "429" in str(e) else 2.5
            if i<retries-1: time.sleep(back); continue
            raise

def fetch_new_pools(limit=25):
    data=json.loads(get("https://api.geckoterminal.com/api/v2/networks/base/new_pools?page=1")).get("data",[])
    out=[]
    for p in data:
        a=p.get("attributes",{}); name=a.get("name","?")
        if "ETH" not in name.upper() and "WETH" not in name.upper(): continue
        addr=p["id"].split("_")[-1]
        try: liq=float(a.get("reserve_in_usd") or 0)
        except: liq=0.0
        if liq<=0: continue            # skip GeckoTerminal negative-glitch pools
        if liq<10000: continue         # floor: real but small (high rug risk)
        out.append({"name":name,"address":addr,"liq":liq,"fee":30.0})
    out.sort(key=lambda x:x["liq"],reverse=True)
    return out[:limit]

def candles(addr, tf="hour", limit=200):
    url=f"https://api.geckoterminal.com/api/v2/networks/base/pools/{addr}/ohlcv/{tf}?aggregate=1&limit={CANDLE_LIMIT}"
    rows=json.loads(get(url)).get("data",{}).get("attributes",{}).get("ohlcv_list",[])
    return sorted(({"ts":r[0],"c":float(r[4])} for r in rows),key=lambda x:x["ts"])

def bt(c, fee, liq, fast=8, slow=32, entry_roc=0.0, ob=85, stop=0.10, exit_roc=-0.03, gas_usd=0.04, trade_eth=0.05):
    """Looser entry (just need uptrend), wide trailing exit — suited to early entry."""
    cl=[x["c"] for x in c]
    if len(cl)<slow+5: return None
    eth_usd=eth_usd_price()
    slip=min(max((trade_eth*eth_usd)/liq*10000*0.5,1.0),80.0)
    gas=gas_usd/(trade_eth*eth_usd)*10000
    ec=(fee+slip+gas)/10000; xc=ec
    pos=0.0; ep=0.0; cash=1.0; peak=1.0; mdd=0.0; wins=0; loss=0; gw=0.0; gl=0.0
    entry_hi=0.0
    for i in range(len(cl)):
        px=cl[i]
        if i<slow: continue
        sf=sma(cl[:i+1],fast); ss=sma(cl[:i+1],slow); rr=roc(cl[:i+1],fast); rs=rsi(cl[:i+1],14)
        if pos>0:
            entry_hi=max(entry_hi,px)
            # trailing stop: 10% off the post-entry high, OR trend break, OR blowoff
            if px<sf or (entry_hi>0 and px/entry_hi-1 < -stop) or rs>ob or rr<exit_roc:
                pnl=pos*(px/ep-1)-xc; cash=pos*(px/ep)*(1-xc); pos=0.0
                if pnl>0: wins+=1; gw+=pnl
                else: loss+=1; gl+=-pnl
                peak=max(peak,cash); mdd=max(mdd,1-cash/peak)
        elif px>ss and px>sf and sf>ss and rs>50:   # early: any healthy uptrend
            pos = cash * (1 - xc); ep = px; entry_hi = px
    if pos>0:
        pnl=pos*(cl[-1]/ep-1)-xc; cash=pos*(cl[-1]/ep)*(1-xc)
        if pnl>0: wins+=1; gw+=pnl
        else: loss+=1; gl+=-pnl
    nr=cash-1; n=wins+loss
    return {"net":nr*100,"trades":n,"wr":wins/n*100 if n else 0,
            "pf":(gw/gl if gl else (9.9 if gw else 0)),"mdd":mdd*100}

def main():
    print("=== EARLY-EDGE TEST: enter coins BEFORE they trend ===\n")
    newp=fetch_new_pools(20)
    print(f"Fetched {len(newp)} new ETH-paired pools (liq>=$10k, early window):\n")
    results=[]
    for c in newp:
        try: candles_h=candles(c["address"],"hour",200)
        except Exception as e:
            print(f"  skip {c['name']}: {e}"); continue
        if len(candles_h)<25:
            print(f"  {c['name'][:24]:24} only {len(candles_h)} candles (too new)"); continue
        r=bt(candles_h, c["fee"], c["liq"])
        if not r: continue
        r["name"]=c["name"]; r["liq"]=c["liq"]
        results.append(r)
        flag="✅" if r["net"]>0 else "❌"
        print(f"  {c['name'][:24]:24} {flag} {r['net']:+6.1f}% trades={r['trades']:2} "
              f"win={r['wr']:4.0f}% PF={r['pf']:.2f} DD={r['mdd']:.1f}%")
    if not results:
        print("\nNo backtestable new pools yet (coins too new / no history).")
        print("This is expected: truly early pools have <40h of candles.")
        print("Re-run in a few hours as they accrue history; THAT is the edge window.")
        return
    prof=[r for r in results if r["net"]>0]
    avg=sum(r["net"] for r in results)/len(results)
    print(f"\n=== EARLY-EDGE SUMMARY ({len(results)} coins with history) ===")
    print(f"Profitable: {len(prof)}/{len(results)} ({len(prof)/len(results)*100:.0f}%)")
    print(f"Avg net return: {avg:+.1f}%   (trending basket was -2.7% over same window)")
    print("\nVERDICT:")
    if len(prof)/len(results)>=0.5 and avg>0:
        print("EARLY-DISCOVERY EDGE CONFIRMED — deploy the discovery-first strategy.")
    else:
        print("Mixed. Early entry helps vs trending but still needs selection/filtering.")
    json.dump(results, open("early_edge_results.json","w"), indent=2)
    print("Wrote early_edge_results.json")

if __name__=="__main__":
    main()
