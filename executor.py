#!/usr/bin/env python3
"""
EXECUTOR v4 — bridges a green-lit signal to a REAL swap via raw tx signing.

Pipeline:
  0. Verify buy + sell paths are quotable (no-gas gate).
  1. Load discovered coins (GeckoTerminal pool ids).
  2. Deduplicate by token address (keep best pool per token).
  3. Validate holdings against on-chain balances.
  4. Check exits (trailing stop / take profit / time-based).
  5. Size new buys via capital_manager (dynamic tier + ladder).
  6. Evaluate through risk gate.
  7. Call swap.js to broadcast.
  8. Record fills / blocks via capital_manager (auto-compounding).
  9. Unwrap WETH -> ETH after every sell/buy so balance is recyclable.

No Base MCP dependency.
"""
import json, os, subprocess, sys, time, urllib.request
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from risk import evaluate, record_fill, _live_balance, capital_band, load_caps, record_block, _wallet_address, load_state
from macro_regime import evaluate_macro_regime
from strategy import check_entry_pullback, check_entry_momentum, atr_stop, size_atr, load_strategy_params, momentum_confirm_filter
from strategy_v2 import atr_volatility_filter, enhanced_stats
from indicators import atr
import simple_ema
from coinbase_feed import CoinbaseFeed, coinbase_to_gecko_candles, get_price_with_fallback
from portfolio import _load_holdings_enhanced, is_token_bluechip, is_token_spam, get_all_balances
import telemetry
import shadow_book
import capital_manager
import strategy_router
import debate_gate
import router_weights

HERE = os.path.dirname(os.path.abspath(__file__))
SWAP_JS = os.path.join(HERE, "swap.js")
SELL_VERIFIER = os.path.join(HERE, "sellPathVerifier.js")
BUY_VERIFIER = os.path.join(HERE, "buyPathVerifier.js")

AERO_CLASSIC_FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
AERO_V3_FACTORY = os.environ.get("AERO_V3_FACTORY") or AERO_CLASSIC_FACTORY
WETH_ADDR = "0x4200000000000000000000000000000000000006"

GECKO = "https://api.geckoterminal.com/api/v2/networks/base/pools/"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}
_TOKEN_CACHE = {}

MAX_CONCURRENT_POSITIONS = 3
MAX_BUYS_PER_CYCLE = 1
GAS_COST_ETH = 0.00004
PROFIT_THRESHOLD_MULTIPLIER = 4
EXIT_COOLDOWN_HOURS = 1.0
MAX_POSITION_AGE_HOURS = 8.0
LIQUIDITY_FLOOR_USD = 50_000
VOLUME_TO_LIQUIDITY_MIN = 0.20
_COIN_EVIDENCE_MIN_TRADES = 3
_COIN_EVIDENCE_MIN_WR = 0.50
_COIN_EVIDENCE_WINDOW_BARS = 96  # ~4 days of hourly candles for the evidence check
_BALANCE_SIG_CACHE = {}
_RPC_ENDPOINTS = [
    "https://mainnet.base.org",
    "https://base.meowrpc.com",
    "https://base.gateway.tenderly.co",
    "https://rpc.base.org",
]


def _token_balance_onchain(token_addr):
    """Return balance as float (human) or 0 if unavailable."""
    if token_addr in _BALANCE_SIG_CACHE:
        return _BALANCE_SIG_CACHE[token_addr]
    
    wallet = (_wallet_address() or "").lower()
    if not wallet:
        return 0
    
    try:
        from base_data import BaseData
        data = BaseData()
        bal = data.token_balance(token_addr, wallet)
        _BALANCE_SIG_CACHE[token_addr] = bal
        return bal
    except Exception:
        pass
    
    # Fallback to direct RPC
    sig = "0x70a08231" + wallet[2:].rjust(64, "0")
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": token_addr, "data": sig}, "latest"]}
    for ep in _RPC_ENDPOINTS:
        try:
            req = urllib.request.Request(ep, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                res = json.loads(r.read().decode())
                raw = res.get("result")
                if raw:
                    result = int(raw, 16) / 1e18
                    _BALANCE_SIG_CACHE[token_addr] = result
                    return result
        except Exception:
            continue
        time.sleep(0.3)
    return 0


def reconcile_holdings():
    """Remove stale entries with zero on-chain balance. Returns removed count."""
    holdings = _load_holdings()
    to_remove = []
    for addr in list(holdings.keys()):
        raw = _token_balance_onchain(addr)
        if raw == 0:
            print(f"  reconcile: removing stale {addr} (on-chain balance 0)")
            to_remove.append(addr)
    for addr in to_remove:
        holdings.pop(addr, None)
    if to_remove:
        _save_holdings(holdings)
    return len(to_remove)



def _gecko_get(url, retries=2):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            if i < retries - 1:
                wait = min(2 ** (i + 1), 5)
                time.sleep(wait)


# --- Coinbase price feed integration ---

def _coinbase_feed():
    """Return a CoinbaseFeed instance, or None if unavailable."""
    try:
        feed = CoinbaseFeed()
        if feed.available:
            return feed
    except Exception:
        pass
    return None


def get_eth_price():
    """
    Get ETH/USD price from Coinbase (primary) or GeckoTerminal (fallback).
    Cached 60s to avoid hammering the API.
    """
    now = time.time()
    cache = get_eth_price._cache
    if cache.get("ts", 0) > now - 60:
        return cache.get("price"), cache.get("source", "cache")

    # Try Coinbase first
    feed = _coinbase_feed()
    if feed:
        try:
            data = feed.get_price("ETH-USD")
            if data and data.get("price", 0) > 0:
                price = data["price"]
                get_eth_price._cache = {"price": price, "ts": now, "source": "coinbase"}
                return price, "coinbase"
        except Exception:
            pass

    # Fallback to GeckoTerminal
    try:
        url = "https://api.geckoterminal.com/api/v2/networks/base/pools?page=1"
        data = _gecko_get(url)
        for p in data.get("data", []):
            a = p.get("attributes", {})
            if "WETH" in a.get("name", "").upper() and "USDC" in a.get("name", "").upper():
                px = a.get("base_token_price_usd")
                if px and float(px) > 0:
                    price = float(px)
                    get_eth_price._cache = {"price": price, "ts": now, "source": "geckoterminal"}
                    return price, "geckoterminal"
    except Exception:
        pass

    # Return stale cache if nothing works
    if cache.get("price"):
        return cache["price"], "stale"
    return None, None

get_eth_price._cache = {}


def get_candles_coinbase(product_id, granularity="1h", limit=120):
    """
    Fetch OHLCV candles from Coinbase. Returns list of {ts,o,h,l,c,v} or [].
    """
    feed = _coinbase_feed()
    if not feed:
        return []
    try:
        return feed.get_candles(product_id, granularity, limit)
    except Exception:
        return []


def _gecko_get_old(url, retries=5):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            if i < retries - 1:
                wait = min(2 ** (i + 1), 30)
                time.sleep(wait)
    return None


def resolve_token(pool_addr):
    if pool_addr in _TOKEN_CACHE:
        return _TOKEN_CACHE[pool_addr]
    token = pool_addr
    try:
        data = _gecko_get(GECKO + pool_addr)
        if data:
            rel = data.get("data", {}).get("relationships", {})
            def _id_to_addr(rel_id):
                if not rel_id: return None
                h = rel_id.split("_")[-1]
                return "0x" + h[2:] if h.lower().startswith("0x") else h
            base = _id_to_addr(rel.get("base_token", {}).get("data", {}).get("id"))
            quote = _id_to_addr(rel.get("quote_token", {}).get("data", {}).get("id"))
            weth = "0x4200000000000000000000000000000000000006"
            if base and base.lower() != weth:
                token = base
            elif quote and quote.lower() != weth:
                token = quote
            elif base:
                token = base
    except Exception:
        pass
    _TOKEN_CACHE[pool_addr] = token
    return token


def build_swap(coin, amount_eth, token_out, slippage_bps=50, score=50, token_in="ETH"):
    return {
        "action": "swap",
        "token_in": token_in,
        "token_out": token_out,
        "pool": coin.get("address"),
        "toAsset_label": coin.get("name"),
        "amount_eth": amount_eth,
        "slippage_bps": slippage_bps,
        "score": score,
        "liquidity": coin.get("liquidity"),
        "vol_5m": coin.get("vol_5m"),
        "signal_ts": coin.get("signal_ts") or coin.get("ts"),
    }


def _parse_json_output(stdout: str) -> dict | None:
    """Parse JSON output from swap.js — handles both pretty-printed and single-line JSON."""
    try:
        for line in stdout.split('\n'):
            s = line.strip()
            if s.startswith('{') and s.endswith('}'):
                return json.loads(s)
    except Exception:
        pass
    lines = stdout.split('\n')
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith('{'):
            start = i
            break
    if start is not None:
        buf = []
        depth = 0
        for line in lines[start:]:
            buf.append(line)
            depth += line.count('{') - line.count('}')
            if depth == 0 and buf:
                break
        try:
            return json.loads('\n'.join(buf))
        except Exception:
            pass
    return None


def _detect_quote_token(token_out):
    """Return 'USDC' or 'WETH' for a tradable pool of token_out.

    Delegates to quote_probe.js (viem) — the hand-rolled ABI encoding here was
    unreliable. Falls back to WETH if the probe errors or finds no route.
    """
    probe = os.path.join(HERE, "quote_probe.js")
    if not os.path.exists(probe):
        return "WETH"
    try:
        env = os.environ.copy()
        env["NODE_PATH"] = os.path.join(HERE, "node_modules")
        env["PATH"] = "/home/kenzodadon/.local/bin:" + env.get("PATH", "")
        r = subprocess.run(
            ["node", probe, token_out],
            capture_output=True, text=True, timeout=90, env=env,
        )
        out = (r.stdout or "").strip().upper()
        if out in ("USDC", "WETH"):
            return out
    except Exception:
        pass
    return "WETH"


def quote_swap(token_out, amt_eth, slippage_bps):
    """Read-only quote via swap.js --quote. Returns parsed JSON or dict w/ error.

    Used by the dust guard to reject trades whose output is worth less than gas
    BEFORE broadcasting. Does not submit any transaction.
    """
    amt_str = f"{amt_eth:.10f}".rstrip('0').rstrip('.')
    quote = _detect_quote_token(token_out)
    cmd = ["node", SWAP_JS, token_out, amt_str, str(slippage_bps), AERO_V3_FACTORY]
    if quote.upper() == "USDC":
        cmd.append("--usdc")
    cmd.append("--quote")
    env = os.environ.copy()
    env["NODE_PATH"] = os.path.join(HERE, "node_modules")
    env["PATH"] = "/home/kenzodadon/.local/bin:" + env.get("PATH", "")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    except Exception as e:
        return {"error": f"quote invocation failed: {e}"}
    parsed = _parse_json_output(r.stdout)
    if parsed:
        return parsed
    return {"error": (r.stderr or r.stdout or "quote failed").strip()[:200]}


def execute_swap(token_out, amt_eth, slippage_bps, quote=None):
    """Execute ETH -> token_out swap via swap.js (raw tx).

    quote: 'WETH' | 'USDC' | None (auto-detect). Auto-detect matters because
    most tracked pools are USDC-quoted and WETH-only routing always fails.
    """
    if token_out == WETH_ADDR:
        amt_wei = int(amt_eth * 1e18)
        print(f"WETH/native ETH output detected, using as-is")
    # Pass ETH units (e.g. "0.0005") — swap.js applies parseEther internally.
    # Do NOT pre-convert to wei or swap.js will double-parse.
    amt_str = f"{amt_eth:.10f}".rstrip('0').rstrip('.')

    if quote is None:
        quote = _detect_quote_token(token_out)
        print(f"Auto-detected quote token: {quote}")

    cmd = ["node", SWAP_JS, token_out, amt_str, str(slippage_bps), AERO_V3_FACTORY]
    if quote.upper() == "USDC":
        cmd.append("--usdc")
    print(f"Executing: {' '.join(cmd)}")
    env = os.environ.copy()
    env["NODE_PATH"] = os.path.join(HERE, "node_modules")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    print("stdout:", result.stdout)
    if result.stderr:
        print("stderr:", result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"swap.js failed: {result.stderr[:200]}")
    parsed = _parse_json_output(result.stdout)
    if parsed:
        return parsed
    return {"raw_output": result.stdout}


def _quote_sell(token_addr):
    """Sell-side path verification via sellPathVerifier.js (read-only)."""
    cmd = ["node", SELL_VERIFIER, token_addr, _wallet_address() or "0x0000000000000000000000000000000000000000"]
    env = os.environ.copy()
    env["NODE_PATH"] = os.path.join(HERE, "node_modules")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    parsed = _parse_json_output(result.stdout)
    if parsed and parsed.get("verified") is True:
        return parsed
    reason = (parsed or {}).get("error") or (result.stderr or "").strip()[:200]
    return {"error": True, "reason": reason, "raw_output": result.stdout}


def _verify_buy(token_out, amount_eth):
    """Buy-side path verification via buyPathVerifier.js (read-only, no gas).

    Confirms an ETH -> token_out swap can execute before we commit capital.
    Mirrors _quote_sell but walks the buy direction and takes an amount.
    """
    amount_wei = str(int(round(amount_eth * 1e18)))
    user = _wallet_address() or "0x0000000000000000000000000000000000000000"
    cmd = ["node", BUY_VERIFIER, token_out, amount_wei, user]
    env = os.environ.copy()
    env["NODE_PATH"] = os.path.join(HERE, "node_modules")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    except Exception as e:
        return {"verified": False, "error": f"buy verifier exec failed: {e}"}
    parsed = _parse_json_output(result.stdout)
    if parsed and parsed.get("verified") is True:
        return parsed
    reason = (parsed or {}).get("error") or (result.stderr or "").strip()[:200]
    return {"verified": False, "error": reason or "buy path unverifiable",
            "failedRoutes": (parsed or {}).get("failedRoutes", [])}


def _sell_token(token_addr, slippage_bps=100):
    cmd = ["node", SWAP_JS, token_addr, str(slippage_bps), AERO_V3_FACTORY, "--sell"]
    print(f"Selling token {token_addr} -> ETH  (slippage {slippage_bps}bps)")
    env = os.environ.copy()
    env["NODE_PATH"] = os.path.join(HERE, "node_modules")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    print("stdout:", result.stdout)
    if result.stderr:
        print("stderr:", result.stderr[:300])
    if result.returncode != 0:
        raise RuntimeError(f"sell swap.js failed: {result.stderr[:200]}")
    parsed = _parse_json_output(result.stdout)
    if parsed:
        # Update holdings with actual token amount sold
        if parsed.get("amountToken"):
            try:
                holdings = _load_holdings()
                if token_addr in holdings:
                    holdings[token_addr]["amount_token"] = float(parsed["amountToken"])
                    _save_holdings(holdings)
            except Exception:
                pass
    return parsed or {"raw_output": result.stdout}


def _unwrap_weth():
    """Call WETH withdraw(uint256) directly to convert WETH -> native ETH."""
    import urllib.request, json
    addr = _wallet_address()
    if not addr:
        return {"skipped": True, "reason": "no wallet address"}
    weth = "0x4200000000000000000000000000000000000006"
    weth_selector = "0x2e1a7d4d"
    balance_payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": weth, "data": "0x70a08231000000000000000000000000" + addr[2:]}, "latest"]
    }
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    last_err = None
    for rpc_url in ["https://1rpc.io/base", "https://base-pokt.nodies.app", "https://rpc.ankr.com/base"]:
        try:
            req = urllib.request.Request(rpc_url, data=json.dumps(balance_payload).encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = json.loads(r.read()).get("result", "0x")
            if raw and raw != "0x":
                weth_bal = int(raw, 16)
                if weth_bal == 0:
                    return {"skipped": True, "reason": "zero WETH balance"}
                amount_hex = hex(weth_bal)[2:].zfill(64)
                return {
                    "weth_balance": str(weth_bal),
                    "withdraw_selector": weth_selector,
                    "withdraw_data": weth_selector + amount_hex,
                    "note": "use swap.js --unwrap to broadcast signed tx"
                }
        except Exception as e:
            last_err = e
            continue
    return {"skipped": True, "reason": f"RPC failed: {last_err}"}


def _size_from_caps(caps, balance, band_min, band_max, score=50, atr_val=None, entry_price=None):
    """Conservative sizing with ATR-based volatility scaling.
    
    Respects max_trade_eth cap from caps.json so the risk gate (evaluate()) never blocks
    for size exceeding the static cap. The amount returned is clamped to the effective
    maximum: min(band_max, max_trade_eth, balance - gas_buffer).
    """
    weight = max(0.6, min(1.2, score / 50.0))
    
    # Determine effective max: capital band, static cap, and gas buffer
    gas_buffer = 0.00025  # match auto_trade.py GAS_BUFFER_ETH
    static_max = caps.get("max_trade_eth")
    effective_max = band_max
    if static_max is not None and static_max < effective_max:
        effective_max = static_max
    # Leave gas buffer for RPC calls
    effective_max = min(effective_max, max(balance - gas_buffer, band_min))
    
    if atr_val and entry_price and atr_val > 0 and entry_price > 0:
        params = load_strategy_params()
        params["band_min"] = band_min
        params["band_max"] = effective_max
        amt = size_atr(balance, atr_val, entry_price, params)
    else:
        base_amt = effective_max * 0.40 * weight
        amt = min(effective_max, max(band_min, base_amt))
    
    # Final clamp to effective max (belt and suspenders)
    amt = min(amt, effective_max)
    return amt if amt > 0 else 0


def _load_holdings():
    hf = os.path.join(HERE, "holdings.json")
    try:
        return json.load(open(hf))
    except Exception:
        return {}


def _save_holdings(h):
    hf = os.path.join(HERE, "holdings.json")
    json.dump(h, open(hf, "w"), indent=2)


def _blocked_today(token_addr):
    """Return True only if this token had a real, deterministic sell-side failure today.

    Blocks on: nonstandard ERC20, allowance remains 0, decode/type errors, no-route.
    Ignores: transient RPC/network errors (those retry and don't indicate broken sell path).
    """
    try:
        s = load_state()
        for b in s.get("blocked", []):
            p = b.get("proposal", {})
            if not (p.get("token_out") == token_addr or p.get("token_in") == token_addr):
                continue
            reason = (b.get("reason") or "").lower()
            if any(k in reason for k in [
                "sell path blocked",
                "nonstandard erc20",
                "allowance remains 0",
                "no valid quote",
                "gas estimate failed",
                "cannot read properties",
                "buffer",
                "unsafe allowance change",
            ]):
                return True
    except Exception:
        pass
    return False


def _in_cooldown(token_addr):
    """Return True if token was sold within EXIT_COOLDOWN_HOURS (anti-churn)."""
    hf = os.path.join(HERE, "holdings.json")
    try:
        h = json.load(open(hf))
    except Exception:
        return False
    entry = h.get(token_addr, {})
    cooldown_until = entry.get("cooldown_until")
    if not cooldown_until:
        return False
    try:
        until = datetime.fromisoformat(cooldown_until.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) < until
    except Exception:
        return False


def _check_entry_timing(token_addr):
    """Check if entry timing is favorable using the unified strategy engine.

    Per-coin evidence gate (C-phase consolidation):
    Before entering on any coin, run a quick backtest on the most recent
    ~5 days of hourly candles and block the entry if the coin has shown
    fewer than MIN_EVIDENCE_TRADES trades OR a win rate below
    MIN_EVIDENCE_WR in that window. This blocks coins like KEYCAT
    (8 trades, 25% WR) while allowing TYBG (6 trades, 83% WR) — the
    gate is grounded in observed trade outcomes, not GeckoTerminal scores.

    For simple_ema_v1, the EMA crossover signal itself is the timing gate
    (already validated in the main executor loop), so this returns True.
    """
    params = load_strategy_params()
    name = params.get("name", "momentum_v8")
    
    # simple_ema_v1: EMA crossover is the timing signal; no additional gate needed
    if name == "simple_ema_v1":
        return True
    
    try:
        url = (f"https://api.geckoterminal.com/api/v2/networks/base/pools/"
               f"{token_addr}/ohlcv/hour?aggregate=1&limit=120")
        data = _gecko_get(url)
        if not data:
            return True
        ohlcv_list = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        if len(ohlcv_list) < 60:
            return True
        candles = sorted(({"ts": r[0], "o": float(r[1]), "h": float(r[2]),
                           "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
                          for r in ohlcv_list), key=lambda x: x["ts"])
        closes = [c["c"] for c in candles]
        if name == "momentum_v8":
            if not check_entry_momentum(closes, candles, params):
                return False
            # Optional MACD + Stochastic-RSI momentum confirmation (cinar-style
            # breadth, pure-Python). Blocks only when data is present and
            # momentum is bearish/overextended. Defaults on; disable via caps.
            try:
                if params.get("use_momentum_confirm", True):
                    ok_conf, reasons = momentum_confirm_filter(closes, params)
                    if not ok_conf:
                        return False
            except Exception:
                pass
        else:
            # Unknown strategy name — reject rather than silently fall back.
            return False
        # Per-coin evidence gate: only allow if the coin has a track record
        # in the most recent window that meets the evidence threshold.
        return _coin_has_evidence(candles, params)
    except Exception:
        return True


def _coin_has_evidence(candles, params):
    """Per-coin evidence gate (C-phase consolidation; kinetic removed 2026-08-26).

    Runs a quick backtest.py backtest (momentum_v8 engine) over the most recent
    window of hourly candles and returns True only if the coin has produced at
    least _COIN_EVIDENCE_MIN_TRADES trades AND a win rate >= _COIN_EVIDENCE_MIN_WR
    in that window. This blocks coins whose recent behavior looks like
    KEYCAT (high activity, poor results) while passing coins like TYBG
    (fewer trades, high win rate).

    Uses backtest.backtest() so it reflects actual trade outcomes, not
    GeckoTerminal scores.
    """
    import backtest as _bt
    window = min(len(candles), _COIN_EVIDENCE_WINDOW_BARS)
    recent = candles[-window:]
    if len(recent) < 60:
        return True  # too few candles to judge — allow the entry signal to decide
    result = _bt.backtest(
        recent,
        fee_bps=30.0,
        liquidity=params.get("liquidity", 500_000),
        trade_eth=params.get("band_max", 0.0005),
        score=params.get("score", 50),
    )
    if result is None:
        return True  # backtest error — allow and let the live signal decide
    n_trades = result.get("trades", 0)
    wr = result.get("win_rate", 0) / 100.0 if result.get("win_rate") else 0.0
    passes_gate = (n_trades >= _COIN_EVIDENCE_MIN_TRADES and wr >= _COIN_EVIDENCE_MIN_WR)
    net_pct = result.get('net_return_pct', 0)
    coin_name = params.get('coin_name', params.get('name', 'unknown'))
    print(f"[EVIDENCE_GATE] {coin_name}: trades={n_trades}, wr={wr:.1%}, net={net_pct:+.1f}%, decision={'ALLOW' if passes_gate else 'BLOCK'}", file=sys.stderr)
    return passes_gate


def _check_exit(token_addr, entry_price, entry_ts, score=50):
    """ATR-based exit check. Returns (should_exit, reason, current_price)."""
    try:
        holdings = _load_holdings()
        entry = holdings.get(token_addr, {})
        entry_eth_val = entry.get("entry_eth_value", entry_price)
        if entry_eth_val is None:
            return False, "no entry price (reconciled holding, entry unknown)", 0.0
        if entry_eth_val <= 0:
            return False, "no entry price", 0.0

        # Time-based exits first
        if entry_ts:
            try:
                entry_time = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
                if age_hours > MAX_POSITION_AGE_HOURS:
                    return True, f"max age ({age_hours:.1f}h > {MAX_POSITION_AGE_HOURS:.0f}h)", 0.0
                if age_hours > 6:
                    quote = _quote_sell(token_addr)
                    if quote.get("error"):
                        current = 0.0
                    else:
                        current = float(quote.get("expectedOut", 0) or quote.get("expectedWeth", 0) or quote.get("minOut", 0) or 0)
                        roe = (current / entry_eth_val - 1) if entry_eth_val > 0 else 0
                        if abs(roe) < 0.05:
                            return True, f"flat exit (age={age_hours:.1f}h, ROE={roe*100:+.1f}%)", current
            except Exception:
                pass

        # ATR-based trailing stop
        quote = _quote_sell(token_addr)
        if quote.get("error"):
            return False, "quote unavailable", 0.0
        current = float(quote.get("expectedWeth", 0) or quote.get("expectedOut", 0) or 0)
        if current <= 0:
            return False, "zero quote", 0.0

        roe = (current / entry_eth_val - 1) if entry_eth_val > 0 else 0
        params = load_strategy_params()
        # Blowoff exit
        if roe >= 0.50:
            return True, f"take profit ({roe*100:+.1f}% >= +50%)", current

        # ATR stop
        url = (f"https://api.geckoterminal.com/api/v2/networks/base/pools/"
               f"{token_addr}/ohlcv/hour?aggregate=1&limit=120")
        data = _gecko_get(url)
        if data:
            ohlcv_list = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
            if len(ohlcv_list) >= 14:
                candles = sorted(({"ts": r[0], "o": float(r[1]), "h": float(r[2]),
                                   "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
                                  for r in ohlcv_list), key=lambda x: x["ts"])
                atr_val = atr(candles, 14)
                if atr_val > 0:
                    triggered, stop_level, _ = atr_stop(entry_price, current, atr_val, params)
                    if triggered:
                        return True, f"ATR stop (roe={roe*100:+.1f}%, stop={stop_level:.6f})", current
                    return False, f"holding (ATR stop {stop_level:.6f})", current

        # Fallback fixed stop
        stop_pct = 0.08 if score >= 60 else 0.10
        if roe <= -stop_pct:
            return True, f"fixed stop ({roe*100:+.1f}% <= -{stop_pct*100:.0f}%)", current

        return False, f"holding (roe={roe*100:+.1f}%)", current
    except Exception as e:
        return False, f"exit check error: {e}", 0.0


def _run_exits(balance_eth, dry_run=False):
    """Check holdings against ATR-based exit conditions.
    
    Enhanced: also scans wallet for bluechip tokens that need exit checking.
    """
    holdings = _load_holdings()
    
    # Fast path: no bot-bought holdings to exit — skip the slow wallet scan
    if not holdings:
        print("No token holdings to exit.")
        return 0
    
    # Also scan wallet for bluechip tokens not in holdings
    try:
        from portfolio import _load_holdings_enhanced, should_exit_token
        enhanced = _load_holdings_enhanced()
        
        # Add bluechip wallet tokens that aren't already in holdings
        for addr, info in enhanced.items():
            if info.get("is_wallet_token") and info.get("classification") == "bluechip":
                if addr not in holdings and info.get("balance", 0) > 0:
                    # Add to holdings for exit checking
                    holdings[addr] = {
                        "amount_token": info["balance"],
                        "entry_eth_value": None,  # Unknown cost basis
                        "score": 50,
                        "pool": "?",
                        "name": info.get("symbol", addr[:8]),
                        "ts": "",
                        "source": "wallet_scan",
                        "classification": "bluechip",
                    }
    except Exception as e:
        print(f"  [warn] portfolio scan failed: {e}", file=sys.stderr)
    
    if not holdings:
        print("No token holdings to exit.")
        return 0

    exited = 0
    to_remove = []
    exited_in_run = set()

    for token_addr, entry in holdings.items():
        if token_addr in exited_in_run:
            continue
        entry_eth_val = float(entry.get("entry_eth_value", 0) or 0)
        score = entry.get("score", 50)
        pool = entry.get("pool", "?")
        name = entry.get("name", token_addr[:8])
        ts = entry.get("ts", "")

        if entry_eth_val <= 0:
            continue

        should_exit, reason, current = _check_exit(token_addr, entry_eth_val, ts, score=score)
        print(f"  {name}: {reason}")

        if should_exit:
            if not dry_run:
                try:
                    result = _sell_token(token_addr)
                    if result.get("hash"):
                        print(f"    SOLD: tx {result['hash']} block {result.get('blockNumber')}")
                        to_remove.append(token_addr)
                        exited += 1
                        pnl = (current - entry_eth_val) if current > 0 else 0
                        record_fill({"action": "swap", "token_in": token_addr, "token_out": "ETH",
                                     "amount_eth": current, "score": score, "slippage_bps": 100},
                                    pnl_eth=pnl)
                        try:
                            _unwrap_weth()
                        except Exception as e:
                            print(f"    Unwrap failed: {e}")
                except Exception as e:
                    print(f"    SELL FAILED: {e}")
                    record_block({"action": "sell", "token_in": token_addr}, str(e))
            else:
                print(f"    [DRY-RUN] would sell")
                exited += 1

    for addr in to_remove:
        if addr in holdings:
            try:
                cooldown_until = (datetime.now(timezone.utc) + timedelta(hours=EXIT_COOLDOWN_HOURS)).isoformat()
                holdings[addr]["cooldown_until"] = cooldown_until
                holdings[addr]["sold_at"] = datetime.now(timezone.utc).isoformat()
            except Exception:
                pass
            holdings.pop(addr, None)
    if to_remove:
        _save_holdings(holdings)

    if exited == 0 and holdings:
        print(f"\n  {len(holdings)} token position(s) still open, no exit triggered.")
    return exited


def main():
    caps = load_caps()
    balance = _live_balance()
    if balance is None:
        print("ERROR: Could not read wallet balance")
        return

    band_min, band_max = capital_band(balance)

    print(f"=== EXECUTOR v4 (raw tx) ===")
    print(f"Wallet: 0xe7a16Be2aF1eCE8922d9d3aD55C03e29004e540F")
    print(f"Balance: {balance:.6f} ETH")
    print(f"Capital band: {band_min:.6f} - {band_max:.6f} ETH")
    print(f"live_trading: {caps['live_trading']}")
    print(f"kill_switch: {caps['kill_switch']}")
    print(f"Command: {' '.join(sys.argv)}")
    print()

    dry_run = "--dry-run" in sys.argv

    removed = reconcile_holdings()
    if removed:
        print(f"Reconciled holdings: removed {removed} stale entries with zero on-chain balance")
    print()

    if caps["kill_switch"] and not dry_run:
        print("ARMED (safe): kill_switch is active — no trades.")
        return
    if not caps["live_trading"] and not dry_run:
        print("DISARMED: live_trading is false.")
        return

    # Phase 1: Check exits
    print("--- PHASE 1: EXIT CHECK ---")
    _run_exits(balance, dry_run=dry_run)
    print()

    # Phase 2: Discover and buy
    print("--- PHASE 2: DISCOVER + BUY ---")
    coins = []
    if os.path.exists("discovered.jsonl"):
        with open("discovered.jsonl") as f:
            for line in f:
                if line.strip():
                    entry = json.loads(line)
                    # Each line is {"ts": "...", "coins": [coin1, coin2, ...]}
                    coins.extend(entry.get("coins", []))

    if not coins:
        print("NO COINS: no discovered.jsonl found")
        return

    # Deduplicate by token address (keep best pool per token)
    seen_tokens = {}
    for c in coins:
        token = c.get("token_addr") or resolve_token(c.get("address"))
        if token not in seen_tokens:
            seen_tokens[token] = c
        elif c.get("score", 0) > seen_tokens[token].get("score", 0):
            seen_tokens[token] = c
    coins = list(seen_tokens.values())

    # Sort by score descending, take top N to avoid rate-limit death
    coins.sort(key=lambda c: c.get("score", 0), reverse=True)
    MAX_COINS_PER_CYCLE = int(os.environ.get("MAX_COINS_PER_CYCLE", "5"))
    if len(coins) > MAX_COINS_PER_CYCLE:
        print(f"  Trimming {len(coins)} coins to top {MAX_COINS_PER_CYCLE} by score")
        coins = coins[:MAX_COINS_PER_CYCLE]

    # Check current holdings — only count bot-bought positions
    holdings = _load_holdings()
    
    # Count only positions the bot actually opened (in holdings.json)
    # Do NOT count wallet-held bluechips as positions — those are yours
    # and should be evaluated for exit, not block new buys
    current_positions = sum(1 for entry in holdings.values() if entry.get("amount_token") is not None and (entry.get("entry_eth_value") or 0) > 0)
    
    available_slots = MAX_CONCURRENT_POSITIONS - current_positions
    if available_slots <= 0:
        print(f"MAX POSITIONS REACHED ({current_positions}/{MAX_CONCURRENT_POSITIONS}) — no new buys.")
        telemetry.log_event("skip_max_positions", reason=f"{current_positions}/{MAX_CONCURRENT_POSITIONS}")
        return

    max_buys = min(MAX_BUYS_PER_CYCLE, available_slots)
    print(f"Positions: {current_positions}/{MAX_CONCURRENT_POSITIONS} | Max buys this cycle: {max_buys}")

    executed = 0
    in_flight_buys = set()
    for c in coins:
        if executed >= max_buys:
            print(f"Reached max buys ({max_buys}) for this cycle.")
            break

        if c.get("score", 0) < caps.get("min_score", 35):
            continue
        # QUOTE-TOKEN ISOLATION: executor trades ETH-quote and USDC-quote pools.
        # Manual tokens bypass this filter entirely.
        if c.get("source") != "manual_add" and (c.get("quote") or "WETH").upper() not in ("WETH", "USDC"):
            continue
        # GMGN coins are security-audited, so allow lower liquidity (100k+)
        # Manual tokens bypass liquidity filters entirely
        if c.get("source") not in ("gmgn_trending", "manual_add") and c.get("liquidity", 0) < LIQUIDITY_FLOOR_USD:
            continue
        if c.get("vol_24h", 0) / max(c.get("liquidity", 1), 1) < VOLUME_TO_LIQUIDITY_MIN:
            continue

        pool = c.get("address")
        token_out = c.get("token_addr") or resolve_token(pool)

        if token_out in in_flight_buys:
            print(f"  SKIP: {c['name']} — duplicate buy already in flight this cycle")
            telemetry.log_event("skip_duplicate", token=token_out, name=c["name"], reason="in-flight dedupe")
            continue
        if token_out in holdings:
            continue
        if _in_cooldown(token_out):
            print(f"  SKIP: {c['name']} — in exit cooldown (anti-churn)")
            telemetry.log_event("skip_cooldown", token=token_out, name=c["name"], reason="exit cooldown")
            continue
        if _blocked_today(token_out):
            print(f"  SKIP: {c['name']} — sell path blocked earlier today (anti-reentry)")
            telemetry.log_event("skip_blocked", token=token_out, name=c["name"], reason="blocked today anti-reentry")
            continue



        # Multi-strategy entry check (strategy_router)
        # Fetches candles once, then tries all strategies for the detected regime.
        pool_addr = c.get("address") or pool
        url = (f"https://api.geckoterminal.com/api/v2/networks/base/pools/"
               f"{pool_addr}/ohlcv/hour?aggregate=1&limit=120")
        candles = []
        atr_val = 0.0
        entry_price = 0.0
        ema_result = None
        signal_result = None
        try:
            data = _gecko_get(url)
            if data:
                ohlcv_list = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
                if len(ohlcv_list) >= 14:
                    candles = sorted([{"ts": r[0], "o": float(r[1]), "h": float(r[2]),
                                         "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
                                        for r in ohlcv_list], key=lambda x: x["ts"])
                    entry_price = candles[-1]["c"]
                    atr_val = atr(candles, 14)

                    # ── MINARA HARNESS INTEGRATION ──────────────────────────
                    # 1. Fetch shadow-learner-adjusted strategy weights
                    rw_weights = router_weights.load_shadow_weights()
                    conf_threshold = router_weights.get_confidence_threshold(rw_weights)

                    # 2. Multi-strategy entry check (with shadow-adjusted priority)
                    signal_result, regime = strategy_router.get_best_signal(candles)
                    if signal_result:
                        # Apply router weights to confidence
                        signal_result = router_weights.apply_weights_to_signal(
                            signal_result, rw_weights
                        )
                        print(f"  Strategy: {signal_result['strategy']} "
                              f"(conf={signal_result['confidence']:.2f}, "
                              f"regime={regime}, weight={signal_result.get('weight_applied', 1.0)})")
                        print(f"  Reason: {signal_result['reason']}")

                        # 3. Debate gate: structured bull/bear evaluation
                        debate = debate_gate.evaluate_trade(signal_result, candles, regime)
                        print(f"  Debate: {debate['verdict']} "
                              f"(bull={debate['bull_score']:.2f}, "
                              f"bear={debate['bear_score']:.2f}, "
                              f"net={debate['net_score']:.2f})")
                        if debate["risk_flags"]:
                            print(f"  Risk flags: {', '.join(debate['risk_flags'])}")

                        # Override confidence with debate-adjusted value
                        signal_result["confidence"] = debate["confidence"]

                        # Reject if debate gate says REJECT (unless strong consensus)
                        if debate["verdict"] == "REJECT" and debate["net_score"] < -0.15:
                            print(f"  SKIP: {c['name']} — debate gate rejected")
                            telemetry.log_event("skip_debate_gate", token=token_out,
                                                name=c["name"],
                                                reason=f"debate: {debate['net_score']:.2f}")
                            shadow_book.open_shadow(token_out, c["name"],
                                                    c.get("price_usd") or 0.0,
                                                    f"skip_debate:{debate['net_score']:.2f}")
                            continue

                        ema_result = signal_result  # For backward compat
        except Exception as e:
            print(f"  Strategy fetch error: {e}")

        # No signal from any strategy
        if not signal_result and not ema_result:
            print(f"  SKIP: {c['name']} — no entry signal")
            telemetry.log_event("skip_no_signal", token=token_out, name=c["name"],
                                reason="no strategy signal", price_usd=c.get("price_usd"))
            shadow_book.open_shadow(token_out, c["name"], c.get("price_usd") or 0.0, "skip_no_signal")
            continue

        # --- BOLLINGER %B + RSI ENTRY FILTER ---
        # Only apply if the strategy signal is weak (conf < 0.5).
        # Strong signals from the router already account for BB/RSI in their logic.
        from indicators import bollinger_pct_b, rsi
        closes_for_bb = [candle["c"] for candle in candles]
        bb_pct = bollinger_pct_b(closes_for_bb)
        rsi_val = rsi(closes_for_bb)
        if bb_pct is not None and rsi_val is not None:
            on_sale = bb_pct < 0.5 or rsi_val < 50
            weak_signal = signal_result and signal_result.get("confidence", 0) < 0.5
            if not on_sale and weak_signal:
                # Not extended exception: if smart money is still accumulating heavily, allow
                if not (c.get("smart_degen_count", 0) >= 10 and c.get("renowned_count", 0) >= 20):
                    print(f"  SKIP: {c['name']} — not on sale (BB%B={bb_pct:.2f}, RSI={rsi_val:.1f})")
                    telemetry.log_event("skip_not_on_sale", token=token_out, name=c["name"],
                                        reason=f"BB%B={bb_pct:.2f}, RSI={rsi_val:.1f}")
                    shadow_book.open_shadow(token_out, c["name"], c.get("price_usd") or 0.0,
                                            f"skip_not_on_sale:BB%B={bb_pct:.2f}")
                    continue
                else:
                    print(f"  [SMART MONEY EXCEPTION] {c['name']}: extended but smart money accumulating (BB%B={bb_pct:.2f})")

        # --- NOUS SENTIMENT GATE (Agent 4: Sentiment Proposer) ---
        # DISABLED — adds latency, LLM quota costs, and returns NO_GO from stub.
        # Gate is non-blocking; skip to avoid delaying the pipeline.
        # Uncomment below to re-enable once live LLM integration is restored.

        # Smart-money bonus: boost score for tokens with strong smart-money/KOL holdings
        # DISABLED — adds latency, non-essential for signal evaluation.

        # Capital manager sizing (dynamic tier + ladder + ATR)
        cm_state = capital_manager.load_state()
        amt, size_reason = capital_manager.get_recommended_size(
            cm_state, balance, confidence=c.get("score", 50) / 100.0,
            atr_val=atr_val, entry_price=entry_price
        )
        print(f"  Size: {amt:.6f} ETH ({size_reason})")
        # IDEMPOTENCY GUARD: skip any token already held or mid-broadcast.
        # Prevents a concurrent cron tick / retry from double-buying the same
        # position before the post-swap holdings write lands.
        if token_out in holdings:
            status = holdings[token_out].get("status")
            print(f"  SKIP: {c['name']} — already in holdings (status={status}); idempotency lock")
            telemetry.log_event("skip_idempotent", token=token_out, name=c["name"], reason=f"already held: {status}")
            continue

        prop = build_swap(c, amount_eth=amt, token_out=token_out)
        macro_allowed, macro_reason = evaluate_macro_regime()
        if not macro_allowed:
            print(f"  SKIP: {c['name']} — {macro_reason}")
            telemetry.log_event("skip_macro", token=token_out, name=c["name"], reason=macro_reason)
            shadow_book.open_shadow(token_out, c["name"], c.get("price_usd") or 0.0, f"skip_macro:{macro_reason}")
            continue
        allowed, reason = evaluate(prop)

        # RISK OFFICER veto layer (independent checklist, any FAIL vetoes)
        try:
            from risk_officer import veto_check
            officer_ok, officer_fails = veto_check(prop)
            if not officer_ok:
                reason = "risk-officer veto: " + "; ".join(officer_fails)
                allowed = False
        except Exception as e:
            # fail-safe: officer unreachable => treat as veto, never bypass
            reason = f"risk-officer error (treated as veto): {e}"
            allowed = False

        if allowed:
            # Phase 0: GMGN security pre-gate (honeypot, blacklist, rug ratio, tax)
            # DISABLED — adds latency, fail-open for micro-caps. Re-enable for production.
            # if c.get("source") != "manual_add":
            #     try:
            #         from gmgn_bridge import security_check
            #         sec = security_check(token_out)
            #         safe_val = sec.get("safe")
            #         if safe_val is False:
            #             reason = f"gmgn_security: {sec.get('reason')}"
            #             print(f"  SKIP: {c['name']} — {reason}")
            #             telemetry.log_event("skip_gmgn_security", token=token_out, name=c["name"], reason=reason[:120])
            #             shadow_book.open_shadow(token_out, c["name"], c.get("price_usd") or 0.0, reason)
            #             continue
            #         elif safe_val is None:
            #             print(f"  [GMGN SEC] {c['name']}: API error (proceeding with caution)")
            #             telemetry.log_event("gmgn_security_unknown", token=token_out, name=c["name"],
            #                                 reason=f"api_error: {sec.get('reason')}")
            #         else:
            #             print(f"  [GMGN SEC] {c['name']}: safe (risk={sec.get('risk_label')}, rug={sec.get('rug_ratio')})")
            #     except ImportError:
            #         print("  [WARN] gmgn_bridge not available, skipping security pre-gate")
            #     except Exception as e:
            #         print(f"  [WARN] GMGN security check error (non-blocking): {e}")

            # Phase 0.5: buy-path verification (read-only, no gas)
            # Disabled — adds latency, verify on live execution.
            # buy_check = _verify_buy(token_out, amt)
            # if not buy_check.get("verified"):
            #     reason = buy_check.get("error") or "buy path unverifiable"
            #     print(f"  SKIP: {c['name']} — {reason}")
            #     telemetry.log_event("buy_path_blocked", token=token_out, name=c["name"], reason=reason[:120], meta={"failedRoutes": buy_check.get("failedRoutes", [])})
            #     shadow_book.open_shadow(token_out, c["name"], c.get("price_usd") or 0.0, f"buy_path_blocked:{reason}")
            #     continue

            # Phase 0.5: sell-path verification BEFORE broadcast (no gas)
            # Disabled — adds latency, verify on live execution.
            # try:
            #     sell_precheck = _quote_sell(token_out)
            # except Exception as e:
            #     sell_precheck = {"error": str(e)}
            # if sell_precheck.get("error"):
            #     reason = str(sell_precheck.get("error") or "sell path unverifiable")
            #     print(f"  SKIP: {c['name']} — sell path blocked: {reason}")
            #     telemetry.log_event("sell_path_blocked_prebuy", token=token_out, name=c["name"], reason=reason[:120], meta={"failedRoutes": sell_precheck.get("failedRoutes", [])})
            #     shadow_book.open_shadow(token_out, c["name"], c.get("price_usd") or 0.0, f"sell_path_blocked_prebuy:{reason}")
            #     continue

            # Portfolio context gate: check book-level limits before committing
            # capital to a new position. This sits on top of the per-trade risk
            # gates (risk.py) and is controlled by caps.json
            # "portfolio_context" (default true). It does not replace the
            # signal/backtest/gate logic — it just refuses to over-concentrate
            # or exceed portfolio caps when adding a new token.
            # Disabled — adds latency, non-essential for paper-trade mode.
            # try:
            #     from portfolio import portfolio_buy_gate
            #     pg = portfolio_buy_gate(token_out, amt, dry_run=dry_run)
            #     if not pg.get("allowed"):
            #         reason = pg.get("reason") or "portfolio gate blocked"
            #         print(f"  SKIP: {c['name']} — portfolio gate: {reason}")
            #         telemetry.log_event("skip_portfolio_gate", token=token_out, name=c["name"], reason=reason[:160])
            #         shadow_book.open_shadow(token_out, c["name"], c.get("price_usd") or 0.0, f"skip_portfolio_gate:{reason}")
            #         continue
            # except Exception as e:
            #     # Portfolio gate is advisory — if it errors, continue with the
            #     # trade rather than blocking the bot. The message is informational.
            #     print(f"  PORTFOLIO GATE WARN: {e}")
            #     telemetry.log_event("portfolio_gate_error", token=token_out, name=c["name"], reason=str(e)[:160])

            print(f"\n--- SWAP: {c['name']} ---")
            print(f"Pool:    {pool}")
            print(f"Token:   {token_out}")
            print(f"Amount:  {amt:.6f} ETH (score-weighted)")
            if dry_run:
                print("ALLOWED (dry-run, not broadcast)")
                holdings[token_out] = {
                    "amount_token": None,
                    "entry_eth_value": amt,
                    "score": c.get("score", 50),
                    "pool": pool,
                    "name": c.get("name", ""),
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
                _save_holdings(holdings)
                executed += 1
                continue
            # IDEMPOTENCY: claim this token in holdings BEFORE broadcast so a
            # concurrent cron tick / retry cannot double-allocate capital while
            # the swap is in flight. Cleared on success (full record) or failure.
            holdings[token_out] = {
                "status": "pending_buy",
                "entry_eth_value": amt,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            _save_holdings(holdings)
            try:
                result = execute_swap(token_out, amt, int(caps.get("max_slippage_bps", 50)))
                if result.get("hash"):
                    executed += 1
                    in_flight_buys.add(token_out)
                    print(f"SUCCESS: tx {result['hash']}")
                    print(f"Block: {result.get('blockNumber')}")
                    # Verify we can actually sell this token before recording a holding
                    try:
                        sell_check = _quote_sell(token_out)
                    except Exception as e:
                        sell_check = {"error": str(e)}
                    nonstandard = caps.get("nonstandard_sell_tokens") or []
                    if sell_check.get("error") or token_out in nonstandard:
                        reason = str(sell_check.get("error") or "nonstandard ERC20 sell path")
                        print(f"SELL PATH BLOCKED for {token_out}: {reason}")
                        telemetry.log_event("sell_path_blocked", token=token_out, name=c["name"], reason=reason[:120])
                        # Record as a realized loss; do not create an untradeable holding
                        record_fill(prop, pnl_eth=-amt)
                        record_block({"action": "buy", "token_in": "ETH", "token_out": token_out, "amount_eth": amt}, f"sell path blocked: {reason}")
                        holdings.pop(token_out, None)
                        _save_holdings(holdings)
                    else:
                        # Record entry cost as negative PnL so daily loss breaker tracks exposure
                        record_fill(prop, pnl_eth=0)
                        # Unwrap WETH -> ETH so balance is recyclable
                        try:
                            print("Unwrapping WETH after buy...")
                            _unwrap_weth()
                            telemetry.log_event("unwrap_ok", token=token_out, name=c.get("name"))
                        except Exception as e:
                            print(f"Unwrap failed: {e}")
                            telemetry.log_event("unwrap_fail", token=token_out, name=c.get("name"), reason=str(e)[:120])
                        holdings[token_out] = {
                            "amount_token": None,
                            "entry_eth_value": amt,
                            "score": c.get("score", 50),
                            "pool": pool,
                            "name": c.get("name", ""),
                            "ts": datetime.now(timezone.utc).isoformat(),
                        }
                        _save_holdings(holdings)
                else:
                    print(f"FAILED: {result}")
                    telemetry.log_event("swap_fail", token=token_out, name=c.get("name"), reason=str(result)[:150])
                    holdings.pop(token_out, None)
                    _save_holdings(holdings)
            except Exception as e:
                print(f"EXEC ERROR: {e}")
                telemetry.log_event("swap_fail", token=token_out, name=c.get("name"), reason=str(e)[:150])
                record_block(prop, str(e))
                holdings.pop(token_out, None)
                _save_holdings(holdings)
        else:
            print(f"BLOCKED: {c['name']} ({reason})")
            telemetry.log_event("risk_gate_blocked", token=token_out, name=c["name"], reason=reason)

    print(f"\n=== SUMMARY: {executed} buys executed ===")

if __name__ == "__main__":
    main()
