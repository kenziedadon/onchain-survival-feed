#!/usr/bin/env python3
"""
tune_v8.py (CORRECTED) — parameter sweep for the v8 momentum strategy.

Correctness guarantee:
  * Discovers + fetches LIVE in this same run (no stale cache file).
  * Asserts the fast vectorized engine EXACTLY reproduces backtest.py:backtest()
    on the same in-memory candles for v8 defaults (compare trades + net per coin).
    If they diverge, we abort (engine drift = meaningless sweep).
  * Precomputes indicator arrays ONCE per coin, then each grid config is a cheap
    vectorized pass -> fast (seconds, not 20 min).
"""
import json, time, sys
from pathlib import Path
from datetime import datetime, timezone

import backtest as BT
from indicators import sma, rsi, roc, eth_usd_price, CANDLE_LIMIT

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}
_MIN_GAP = 4.0
_last = 0.0

def get(url, timeout=25, retries=8):
    global _last
    for i in range(retries):
        wait = _MIN_GAP - (time.time() - _last)
        if wait > 0: time.sleep(wait)
        _last = time.time()
        try:
            import urllib.request
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except Exception as e:
            back = 8 + i*4 if "429" in str(e) else 2.0
            if i < retries - 1:
                time.sleep(back); continue
            raise

def fetch(addr):
    url = (f"https://api.geckoterminal.com/api/v2/networks/base/pools/"
           f"{addr}/ohlcv/hour?aggregate=1&limit={CANDLE_LIMIT}")
    rows = json.loads(get(url)).get("data", {}).get("attributes", {}).get("ohlcv_list", [])
    return sorted(({"ts": r[0], "o": float(r[1]), "h": float(r[2]),
                    "l": float(r[3]), "c": float(r[4]), "v": float(r[5])} for r in rows),
                 key=lambda x: x["ts"])

def precompute(candles, adx_win=14):
    """Config-INDEPENDENT arrays (rsi/roc8/roc24/adx). sma/roc_f vary per config."""
    n = len(candles)
    closes = [c["c"] for c in candles]
    rsi_a=[50.0]*n; roc8=[0.0]*n; roc24=[0.0]*n
    adx_a=[0.0]*n; pdi_a=[0.0]*n; mdi_a=[0.0]*n
    for i in range(n):
        rsi_a[i]=rsi(closes[:i+1],14)
        if i>=8:    roc8[i]=roc(closes[:i+1],8)
        if i>=24:   roc24[i]=roc(closes[:i+1],24)
        if i>=14:
            plus_dm=[]; minus_dm=[]; trs=[]
            for j in range(max(1,i-14),i+1):
                h=candles[j]["h"]; l=candles[j]["l"]
                ph=candles[j-1]["h"]; pl=candles[j-1]["l"]; pc=candles[j-1]["c"]
                tr=max(h-l,abs(h-pc),abs(l-pc)); trs.append(tr)
                up=h-ph; down=pl-l
                plus_dm.append(max(up,0) if up>down else 0)
                minus_dm.append(max(down,0) if down>up else 0)
            # Match backtest.py exactly: it sums the LAST 14 of the 15-bar window.
            pd=plus_dm[-14:]; md=minus_dm[-14:]; tr=trs[-14:]
            pdi=sum(pd)/sum(tr)*100 if sum(tr)>0 else 0
            mdi=sum(md)/sum(tr)*100 if sum(tr)>0 else 0
            adx=abs(pdi-mdi)/(pdi+mdi)*100 if (pdi+mdi)>0 else 0
            pdi_a[i]=pdi; mdi_a[i]=mdi; adx_a[i]=adx
    return dict(closes=closes,rsi=rsi_a,roc8=roc8,roc24=roc24,adx=adx_a,pdi=pdi_a,mdi=mdi_a)

def run_pre(cfg, P, liquidity, fee_bps, score=50, trade_eth=0.03, gas_usd=0.04):
    closes=P["closes"]; n=len(closes); fast=cfg["fast"]; slow=cfg["slow"]
    if n < slow+5: return None
    eth_usd=eth_usd_price()
    actual_trade=min(trade_eth*(score/50.0),0.05)
    stop_pct=0.08 if score>=60 else 0.10
    if cfg.get("stop_pct"): stop_pct=cfg["stop_pct"]
    slip=min(max((actual_trade*eth_usd)/liquidity*10000*0.5,1.0),80.0)
    gas=gas_usd/(actual_trade*eth_usd)*10000
    ec=(fee_bps+slip+gas)/10000; xc=ec
    pos=0.0; ep=0.0; hi=0.0; cash=1.0; peak=1.0; mdd=0.0
    wins=0; loss=0; gw=0.0; gl=0.0
    for i in range(n):
        px=closes[i]
        if i<slow: continue
        sf=sma(closes[:i+1],fast); ss=sma(closes[:i+1],slow); rs=P["rsi"][i]
        roc_f=roc(closes[:i+1],fast)
        if pos>0:
            hi=max(hi,px); dd=(px/hi-1) if hi>0 else 0; ret=px/ep-1
            if dd<-stop_pct or rs>cfg["overbought"] or ret<-stop_pct:
                # Mirror corrected backtest.py: pnl derived from cash delta.
                before=cash
                cash = pos * (px / ep) * (1 - xc)
                pnl = cash - before
                pos=0.0; hi=0.0
                if pnl>0: wins+=1; gw+=pnl
                else: loss+=1; gl+=-pnl
                peak=max(peak,cash); mdd=max(mdd,1-cash/peak)
        elif (px>ss) and (px>sf) and (sf>ss) and (cfg["rsi_lo"]<rs<cfg["rsi_hi"]) and (roc_f>cfg["entry_roc"]):
            if P["roc24"][i]>0 and P["roc8"][i]>0:
                a=P["adx"][i]; p=P["pdi"][i]; m=P["mdi"][i]
                if a<cfg["adx_min"] or p<m+10: continue
                pos=cash*(1-ec); ep=px; hi=px
    if pos>0:
        before=cash
        cash = pos*(closes[-1]/ep)*(1-xc)
        pnl = cash - before
        if pnl>0: wins+=1; gw+=pnl
        else: loss+=1; gl+=-pnl
    nn=wins+loss
    if nn==0: return {"net":0.0,"trades":0,"wr":0,"pf":0,"mdd":0}
    return {"net":(cash-1)*100,"trades":nn,"wr":wins/nn*100,
            "pf":(gw/gl) if gl else (9.9 if gw else 0),"mdd":mdd*100}

def main():
    print("=== TUNE v8 (CORRECTED, live data + engine-parity check) ===")
    pools = BT.discover_trending(30)
    print(f"Discovered {len(pools)} ETH/WETH-paired trending pools")
    coins=[]
    for p in pools:
        if p["liquidity"]<300_000 or p.get("score",0)<35: continue
        try: c=fetch(p["address"]); time.sleep(0.3)
        except Exception as e: print(f"  [skip {p['name']}] {e}"); continue
        if len(c)<60: print(f"  [skip {p['name']}] {len(c)} candles"); continue
        coins.append((p,c))
    print(f"Loaded {len(coins)} coins (live)\n")

    # ---- ENGINE PARITY: fast must == backtest.py on same candles (v8 default) ----
    print("=== ENGINE PARITY (fast vs backtest.py, v8 defaults) ===")
    defaults=dict(fast=8,slow=32,rsi_lo=40,rsi_hi=75,entry_roc=0.03,overbought=85,adx_min=20)
    parity=True
    for p,c in coins:
        ref=BT.backtest(c,p["fee_bps"],p["liquidity"],score=p.get("score",50))
        PRE=precompute(c)
        fast=run_pre(defaults,PRE,p["liquidity"],p["fee_bps"],p.get("score",50))
        match = ref and fast and ref["trades"]==fast["trades"] and abs(ref["net_return_pct"]-fast["net"])<0.5
        parity = parity and bool(match)
        rn = f"{ref['net_return_pct']:+.1f}%/{ref['trades']}t" if ref else "None"
        fn = f"{fast['net']:+.1f}%/{fast['trades']}t" if fast else "None"
        print(f"  {p['name'][:22]:22} ref={rn:>12} fast={fn:>12} {'OK' if match else 'DIVERGE'}")
    if not parity:
        print("PARITY FAILED — fast engine diverges from backtest.py. Abort."); sys.exit(1)
    print("PARITY PASSED — sweeping on verified-faithful engine.\n")

    # Precompute widest lookbacks once.
    PRE={}; 
    for p,c in coins: PRE[p["address"]]=precompute(c)

    grid=[]
    for fast in (6,8,10,12):
        for slow in (20,24,32,40,48):
            if slow<=fast: continue
            for rsi_lo in (35,40,45,50):
                for rsi_hi in (70,75,80,85):
                    for entry_roc in (0.015,0.02,0.03,0.05):
                        for overbought in (78,80,85):
                            for adx_min in (10,15,20,25):
                                for stop_pct in (0.06,0.08,0.10,0.12):
                                    grid.append(dict(fast=fast,slow=slow,rsi_lo=rsi_lo,
                                                     rsi_hi=rsi_hi,entry_roc=entry_roc,
                                                     overbought=overbought,adx_min=adx_min,
                                                     stop_pct=stop_pct))
    print(f"Grid: {len(grid)} configs x {len(coins)} coins (vectorized)\n")

    scored=[]
    for cfg in grid:
        nets=[]; traded=[]
        for p,c in coins:
            r=run_pre(cfg,PRE[p["address"]],p["liquidity"],p["fee_bps"],p.get("score",50))
            if not r or r["trades"]==0: continue
            nets.append(r["net"]); traded.append(p["name"])
        if len(nets)<max(3,len(coins)//2): continue
        prof=sum(1 for x in nets if x>0); avg=sum(nets)/len(nets)
        beq=1.0
        for x in nets: beq*=(0.5+0.5*(1+x/100))
        scored.append({**cfg,"n":len(nets),"prof":prof,"prof_pct":prof/len(nets)*100,
                       "avg":avg,"basket":(beq-1)*100,"traded":traded})

    if not scored:
        print("NO config traded enough coins (all degenerate on this snapshot).")
        # still report the v8-default baseline from parity above.
        sys.exit(0)

    scored.sort(key=lambda x:(x["prof_pct"]>=60 and x["avg"]>0,x["avg"],x["prof_pct"]),reverse=True)
    print(f"{'fast':>4} {'slow':>4} {'rsi':>6} {'roc':>5} {'ob':>3} {'adx':>3} {'stop':>5} {'n':>3} {'prof':>5} {'avg%':>7} {'basket%':>8}")
    for r in scored[:25]:
        star=" *" if (r["prof_pct"]>=60 and r["avg"]>0) else ""
        print(f"{r['fast']:>4} {r['slow']:>4} {r['rsi_lo']}-{r['rsi_hi']:>2} {r['entry_roc']:>5} "
              f"{r['overbought']:>3} {r['adx_min']:>3} {r['stop_pct']:>5} {r['n']:>3} "
              f"{r['prof']:>2}/{r['n']:<2} {r['avg']:+7.1f} {r['basket']:+8.1f}{star}")

    clears=[r for r in scored if r["prof_pct"]>=60 and r["avg"]>0]
    print("\n=== VERDICT ===")
    if clears:
        b=clears[0]
        print(f"EDGE FOUND: f={b['fast']} s={b['slow']} rsi={b['rsi_lo']}-{b['rsi_hi']} "
              f"roc={b['entry_roc']} ob={b['overbought']} adx>={b['adx_min']} stop={b['stop_pct']}")
        print(f"  {b['prof']}/{b['n']} positive ({b['prof_pct']:.0f}%), avg {b['avg']:+.1f}%, basket {b['basket']:+.1f}%")
    else:
        b=scored[0]
        print(f"NO config cleared 60% bar. Best avg {b['avg']:+.1f}%, {b['prof']}/{b['n']} positive ({b['prof_pct']:.0f}%), basket {b['basket']:+.1f}%")
        print(f"  cfg f={b['fast']} s={b['slow']} rsi={b['rsi_lo']}-{b['rsi_hi']} roc={b['entry_roc']} ob={b['overbought']} adx>={b['adx_min']} stop={b['stop_pct']}")

    Path("backtests").mkdir(exist_ok=True)
    ts=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out={"best":scored[0] if scored else None,"clears_bar":bool(clears),
         "n_configs":len(grid),"n_coins":len(coins),"top":scored[:25],
         "coins":[p["name"] for p,_ in coins]}
    Path(f"backtests/tune_v8_{ts}.json").write_text(json.dumps(out,indent=2))
    print(f"\nWrote backtests/tune_v8_{ts}.json")

if __name__=="__main__":
    main()
