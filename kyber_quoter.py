#!/usr/bin/env python3
"""
kyber_quoter.py — READ-ONLY KyberSwap quote client (third venue beside
Aerodrome and Uniswap V3). Paper-only, disarmed, never signs, never sends.

What it does:
  * Queries the KyberSwap Meta Aggregation [V1] `GET /{chain}/api/v1/routes`
    endpoint for Base (chainId 8453, chain name "base") and returns a normalized
    quote: input/output amounts, USD both sides, gas, per-leg venue detail.
  * Fail-closed router check: if the router address the API returns has no
    CODE on Base, quote() RAISES and returns nothing (same fail-closed
    discipline as simulate.py — an unverified address is never trusted).

What it never does:
  * No build/encode, no swap, no signing, no private key, no state change.
    This is a quoter, not an executor. caps.json (kill_switch=true,
    live_trading=false) is never consulted or written here — nothing here can
    trade regardless of it.

API facts (verified live 2026-10-11 against aggregator-api.kyberswap.com):
  * Endpoint: GET https://aggregator-api.kyberswap.com/base/api/v1/routes
  * Required header: X-Client-Id: <app-name>  (without it, strict rate limits)
  * Response code==0 means "Swap Found". amountOut/amountOutUsd are the quote.
  * Native token is the EEE…Eee sentinel 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE
    (NOT WETH). WETH is 0x4200…0006 and is a normal ERC20 for routing.
  * routeSummary.route[] = list of legs; each leg carries `exchange` (venue id,
    e.g. "aerodrome-cl-3", "uniswap-v3", ...), `pool`, tokenIn/out, amounts.
  * The api.kyberswap.com/swap gateway requires an X-Api-Key (401 without one);
    we deliberately use the keyless aggregator host. If a key is ever supplied
    via env KYBER_API_KEY, the caller may opt into the gateway — default stays
    keyless so nothing silently depends on a secret.

Hand-rolled stdlib only (urllib). No web3, no keys, no third-party deps.
"""

import json
import os
import time
import urllib.request
import urllib.error

# --- verified live on Base 2026-10-11 ---------------------------------------
# KyberSwap Meta Aggregation router returned by the routes API for Base.
ROUTER_BASE = "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5"
RPC = "https://mainnet.base.org"
UA = {"Content-Type": "application/json", "User-Agent": "base-trend-bot/1.0"}
# KyberSwap native-token sentinel (not WETH)
NATIVE_SENTINEL = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

_LAST_CALL = 0.0
_MIN_GAP = 0.5  # KyberSwap keyless tier is rate limited; be polite


def _throttle():
    global _LAST_CALL
    wait = _MIN_GAP - (time.time() - _LAST_CALL)
    if wait > 0:
        time.sleep(wait)
    _LAST_CALL = time.time()


def _http_get(url, headers, timeout=25):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"URLError: {e.reason}")


# ----------------------------- rpc plumbing --------------------------------
def _rpc(method, params, retries=4):
    last = None
    for attempt in range(retries):
        _throttle()
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params}).encode()
        req = urllib.request.Request(RPC, data=body, headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                out = json.loads(r.read().decode())
            if "error" in out:
                last = RuntimeError(out["error"].get("message", str(out["error"])))
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last
            return out.get("result")
        except urllib.error.HTTPError as e:
            last = RuntimeError(f"HTTP {e.code}")
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise last
        except urllib.error.URLError as e:
            last = RuntimeError(f"URLError: {e.reason}")
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise last
    raise last


def router_has_code(addr=ROUTER_BASE):
    """Fail-closed: only trust a KyberSwap route if its router has code on Base.
    A 1-byte non-empty result is CODE (an EOA returns exactly '0x')."""
    code = _rpc("eth_getCode", [addr, "latest"]) or "0x"
    return code != "0x" and len(code) > 2


# ----------------------------- quote client ---------------------------------
class KyberQuoter:
    """Read-only KyberSwap route quoter for Base. Paper-only."""

    def __init__(self, client_id="base-trend-bot", base_url=None, use_gateway=False):
        # Keyless aggregator host by default. use_gateway=True opts into the
        # api.kyberswap.com/swap gateway, which requires KYBER_API_KEY.
        self.client_id = client_id
        self.use_gateway = use_gateway
        if base_url:
            self.base_url = base_url
        elif use_gateway:
            self.base_url = "https://api.kyberswap.com/swap"
        else:
            self.base_url = "https://aggregator-api.kyberswap.com"

    def _headers(self):
        # KyberSwap sits behind Cloudflare which 403s (Error 1010) the default
        # urllib "Python-urllib/3.x" User-Agent. Send a real browser UA.
        h = {
            "X-Client-Id": self.client_id,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) base-trend-bot/1.0",
        }
        if self.use_gateway:
            key = os.environ.get("KYBER_API_KEY")
            if not key:
                raise RuntimeError("gateway selected but KYBER_API_KEY not set")
            h["X-Api-Key"] = key  # value never logged
        return h

    def get_route(self, token_in, token_out, amount_in_wei,
                  gas_include=True, only_direct=False):
        """Query the KyberSwap [V1] routes API. Returns the raw `data` dict or
        raises on any failure (fail-closed — never returns a fake quote)."""
        _throttle()
        params = {
            "tokenIn": token_in.lower(),   # KyberSwap rejects checksummed addrs (4011)
            "tokenOut": token_out.lower(),
            "amountIn": str(amount_in_wei),
            "gasInclude": "true" if gas_include else "false",
        }
        if only_direct:
            params["onlyDirectPools"] = "true"
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{self.base_url}/base/api/v1/routes?{qs}"
        raw = _http_get(url, self._headers())
        payload = json.loads(raw)
        code = payload.get("code")
        if code != 0:
            raise RuntimeError(f"kyber code={code} msg={payload.get('message')!r}")
        data = payload.get("data") or {}
        if "routeSummary" not in data:
            raise RuntimeError("kyber: no routeSummary in response")
        return data

    def quote(self, token_in, token_out, amount_in_wei, **kw):
        """Normalized read-only quote. RAISES if the router has no code on
        Base — an unverified router is never trusted (fail-closed)."""
        data = self.get_route(token_in, token_out, amount_in_wei, **kw)
        rs = data["routeSummary"]
        router = data.get("routerAddress") or ROUTER_BASE
        router_ok = router_has_code(router)
        if not router_ok:
            raise RuntimeError(f"kyber router {router} has NO code on Base — "
                               "refusing to trust route (fail-closed)")
        legs = []
        for leg_group in rs.get("route") or []:
            # route is a list of legs (usually a single-element list)
            group = leg_group if isinstance(leg_group, list) else [leg_group]
            for leg in group:
                legs.append({
                    "exchange": leg.get("exchange"),
                    "pool": leg.get("pool"),
                    "tokenIn": leg.get("tokenIn"),
                    "tokenOut": leg.get("tokenOut"),
                    "swapAmount": leg.get("swapAmount"),
                    "amountOut": leg.get("amountOut"),
                })
        return {
            "venue": "kyberswap",
            "router": router,
            "router_has_code": router_ok,
            "tokenIn": rs.get("tokenIn"),
            "tokenOut": rs.get("tokenOut"),
            "amountIn": rs.get("amountIn"),
            "amountOut": _to_int(rs.get("amountOut")),
            "amountInUsd": _to_float(rs.get("amountInUsd")),
            "amountOutUsd": _to_float(rs.get("amountOutUsd")),
            "gas": rs.get("gas"),
            "gasUsd": _to_float(rs.get("gasUsd")),
            "legs": legs,
            "routeID": data.get("routeID") or rs.get("routeID"),
            "timestamp": rs.get("timestamp"),
        }


def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _to_int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


# ----------------------------- CLI ------------------------------------------
def _main():
    import sys
    # usage: kyber_quoter.py <tokenIn> <tokenOut> <amountInWei>
    if len(sys.argv) < 4:
        print(json.dumps({
            "usage": "kyber_quoter.py <tokenIn> <tokenOut> <amountInWei>",
            "router": ROUTER_BASE,
            "router_has_code": router_has_code(),
        }))
        return
    ti, to, amt = sys.argv[1], sys.argv[2], sys.argv[3]
    q = KyberQuoter().quote(ti, to, amt)
    print(json.dumps(q, indent=2))


if __name__ == "__main__":
    _main()
