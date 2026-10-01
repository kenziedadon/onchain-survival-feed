#!/usr/bin/env python3
"""Walk-forward + benchmark + PF/DD gates for pullback v2"""
import json, time, urllib.request
from datetime import datetime, timezone
from indicators import sma, rsi, roc, eth_usd_price, CANDLE_LIMIT

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}
def get(url, timeout=25, retries=5):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r: return r.read().decode()
        except Exception as e:
            if "429" in str(e) and i<retries-1: time.sleep(2+i*2); continue
            if i<retries-1: time.sleep(1.5); continue
            raise

def discover():
    url = "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1"
    data = json.loads(get(url)).get("data", [])
    out = []
    for p in data:
        a = p.get("attributes", {}); addr = p["id"].split("_")[-1]; name = a.get("name","?")
        if not ("ETH" in name.upper() or "WETH" in name.upper()): continue
        if "USDC" in name.upper() or "USDT" in name.upper() or "DAI" in name.upper(): continue
        liq = float(a.get("reserve_in_usd") or 0); v24h = float(a.get("volume_usd",{}).get("h24") or 0)
        h24 = float(a.get("price_change_percentage",{}).get("h24",0))
        if h24 <= 2 or v24h < 50000 or liq < 300000: continue
        fee = 30.0
        out.append({"name":name,"address":addr,"liquidity":liq,"fee_bps":fee,"score":50})
    return out[:15]

def fetch_ohlcv(addr, limit=300):
    url = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{addr}/ohlcv/hour?aggregate=1&limit={limit}"
    rows = json.loads(get(url)).get("data",{}).get("attributes",{}).get("ohlcv_list",[])
    return sorted(({"ts":r[0],"o":float(r[1]),"h":float(r[2]),"l":float(r[3]),"c":float(r[4]),"v":float(r[5])} for r in rows), key=lambda x: x["ts"])

def buy_and_hold(candles):
    if len(candles)<2: return None
    return (candles[-1]["c"]/candles[0]["c"]-1)*100

def backtest_pullback(candles, fee_bps, liquidity, score=50, trade_eth=0.03, gas_usd=0.04, fast=8, slow=32):
    closes=[c["c"] for c in candles]
    if len(closes)<slow+5: return None
    eth_usd=eth_usd_price()
    actual_trade=min(trade_eth*(score/50.0),0.05)
    stop_pct=0.08 if score>=60 else 0.10
    slip=min(max((actual_trade*eth_usd)/liquidity*10000*0.5,1.0),80.0)
    gas=gas_usd/(actual_trade*eth_usd)*10000
    ec=(fee_bps+slip+gas)/10000; xc=ec
    pos=0.0; entry_px=0.0; entry_hi=0.0; cash=1.0; peak=1.0; mdd=0.0; wins=0; loss=0; gw=0.0; gl=0.0
    for i in range(len(closes)):
        px=closes[i]
        if i<slow: continue
        sf=sma(closes[:i+1],fast); ss=sma(closes[:i+1],slow)
        r=roc(closes[:i+1],fast); rs=rsi(closes[:i+1],14)
        if pos>0:
            entry_hi=max(entry_hi,px)
            dd=(px/entry_hi-1) if entry_hi>0 else 0
            ret=px/entry_px-1
            if dd<-stop_pct or rs>85 or ret<-stop_pct:
                before=cash; cash=pos*(px/entry_px)*(1-xc); pnl=cash-before
                pos=0.0; entry_hi=0.0
                if pnl>0: wins+=1; gw+=pnl
                else: loss+=1; gl+=-pnl
                peak=max(peak,cash); mdd=max(mdd,1-cash/peak)
        elif (px>ss) and (px<sf) and (20<rs<55) and (-0.10<r<0.0):
            roc24=roc(closes[:i+1],24) if len(closes[:i+1])>24 else 0
            if roc24>0:
                pos=cash*(1-ec); entry_px=px; entry_hi=px
    if pos>0:
        before=cash; cash=pos*(closes[-1]/entry_px)*(1-xc); pnl=cash-before
        if pnl>0: wins+=1; gw+=pnl
        else: loss+=1; gl+=-pnl
    n=wins+loss; net=cash-1.0
    return {"net":net*100,"trades":n,"wr":wins/n*100 if n else 0,
            "pf":(gw/gl if gl else (9.99 if gw else 0)),"mdd":mdd*100}

def walk_forward(candles, fee_bps, liquidity, score=50, fast=8, slow=32):
    closes=[c["c"] for c in candles]
    if len(closes)<slow+5: return None
    k=int(len(closes)*0.6)
    tr_closes=closes[:k]; te_closes=closes[k:]
    tr_candles=candles[:k]; te_candles=candles[k:]
    r_tr=backtest_pullback(tr_candles, fee_bps, liquidity, score, fast=fast, slow=slow)
    r_te=backtest_pullback(te_candles, fee_bps, liquidity, score, fast=fast, slow=slow)
    if not r_tr or not r_te: return None
    bh_tr=buy_and_hold(tr_candles)
    bh_te=buy_and_hold(te_candles)
    return {"train_net":r_tr["net"],"test_net":r_te["net"],
            "bh_train":bh_tr,"bh_test":bh_te,
            "pf_test":r_te["pf"],"maxdd_test":r_te["mdd"],
            "trades_test":r_te["trades"]}

def main():
    print("=== WALK-FORWARD + BENCHMARK: PULLBACK v2 ===\n")
    coins = discover()
    print(f"Loaded {len(coins)} trending Base coins\n")
    passed=0; rows=[]
    for c in coins:
        try: candles=fetch_ohlcv(c["address"])
        except Exception as e: print(f"  skip {c['name']}: {e}"); continue
        if len(candles)<60: print(f"  {c['name'][:20]:20} only {len(candles)} candles, skip"); continue
        time.sleep(1.5)
        e=walk_forward(candles, c["fee_bps"], c["liquidity"], score=c.get("score",50))
        if not e: continue
        flags=[]
        if e["test_net"]<=e["bh_test"]: flags.append("FAILS benchmark")
        if e["test_net"]<=0: flags.append("FAILS live (<=0)")
        if e["pf_test"]<1.1: flags.append(f"FAILS PF={e['pf_test']:.2f}")
        if e["maxdd_test"]>40: flags.append(f"FAILS DD={e['maxdd_test']:.0f}%")
        if e["train_net"]-e["test_net"]>30: flags.append("OVERFIT risk")
        v="PASS" if not flags else "REJECT: " + "; ".join(flags)
        if v=="PASS": passed+=1
        rows.append((c["name"],e,v))
        print(f"  {c['name'][:24]:24} train={e['train_net']:+6.1f}% test={e['test_net']:+6.1f}% "
              f"BH_test={e['bh_test']:+6.1f}% PF={e['pf_test']:.2f} DD={e['maxdd_test']:.0f}% trades={e['trades_test']} -> {v}")
    print(f"\n=== VERDICT ===")
    print(f"{passed}/{len(rows)} coins PASS all gates")
    if passed==0:
        print("Pullback v2 still fails benchmark. No live deployment.")
    else:
        print(f"{passed} candidate(s) survived. Incubate 1-2+ months before sizing up.")
    # Save artifact
    artifact={"ts":datetime.now(timezone.utc).isoformat(),"script":"pullback_v2_walkforward",
              "total":len(rows),"passed":passed,"any_passed":passed>0,
              "rows":[{"name":n,"test_net":e["test_net"],"bh_test":e["bh_test"],
                       "pf_test":e["pf_test"],"maxdd_test":e["maxdd_test"],"verdict":v}
                      for n,e,v in rows]}
    json.dump(artifact, open("methodology_eval_pullback.json","w"), indent=2)
    print("Wrote methodology_eval_pullback.json")

if __name__=="__main__":
    main()
