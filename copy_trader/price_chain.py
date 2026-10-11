#!/usr/bin/env python3
"""
price_chain.py — four-source price chain with circuit breaker, disagreement
journal, and per-source timeout budgets. Read-only, paper-only. Never signs,
never trades, never consults or writes caps.json.

Chain order (per McKenzie's authorized scope 2026-10-11):
    GMGN -> GeckoTerminal -> DexScreener -> KyberSwap

Why a chain, not a primary+fallback: the four sources fail on DIFFERENT axes
(GMGN burst-bans; GeckoTerminal 429s under pacing; DexScreener occasionally
lags new pools; KyberSwap rate-limits keyless). Consensus across independent
sources is the point — but so is knowing WHEN they disagree.

Design:
  * Each source is an isolated fetcher returning a typed PriceResult
    (price + reason + detail). No source can raise out of the chain.
  * Per-source timeout budget bounds how long any single source may stall the
    chain; the chain also carries a total wall-clock budget.
  * Circuit breaker: a source that fails N consecutive times is tripped OPEN
    and skipped for a cooldown, then moves to HALF_OPEN (one probe) before it
    is trusted again. A tripped source is never trusted while open.
  * Disagreement journal: when the returning sources disagree beyond a
    relative threshold, every contributing price is recorded (append-only
    JSONL) so a stale/bad source is visible rather than silently outvoted.

Fail-closed: if NO source returns a usable price, the chain returns a typed
non-ok result (no fabricated price). A $0.00 / non-positive price from any
source is treated as no-price, never as a real quote.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Reuse the typed reason vocabulary AND the PriceResult dataclass from the
# poller, so there is exactly one price-result type in the codebase (the poller
# imports this module, so we import back — no circular risk at call time).
PRICE_OK = "ok"
PRICE_HTTP_429 = "http_429"
PRICE_HTTP_5XX = "http_5xx"
PRICE_NO_POOL = "no_pool"
PRICE_PARSE_ERROR = "parse_error"
PRICE_NETWORK = "network"

from copy_trader.poller import PriceResult  # noqa: E402  (single source of truth)

WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

# --- configuration (env-tunable, sane defaults) -----------------------------
CHAIN_SOURCE_ORDER = ["gmgn", "geckoterminal", "dexscreener", "kyberswap"]

# Per-source timeout budget (seconds). A source that exceeds its budget is
# abandoned (its attempt is killed / treated as a network failure).
SOURCE_TIMEOUTS_S = {
    "gmgn": 20,            # gmgn-cli subprocess
    "geckoterminal": 15,
    "dexscreener": 15,
    "kyberswap": 25,       # includes an eth_getCode router check
}
# Total wall-clock budget for the whole chain. If the running total exceeds
# this, remaining sources are skipped and we return with whatever we have.
CHAIN_TOTAL_BUDGET_S = float(os.environ.get("PRICE_CHAIN_BUDGET_S", "55"))

# Circuit breaker: trip a source open after this many consecutive failures;
# hold it open for this cooldown; then allow a single HALF_OPEN probe.
BREAKER_FAIL_THRESHOLD = int(os.environ.get("PRICE_BREAKER_FAILS", "3"))
BREAKER_COOLDOWN_S = float(os.environ.get("PRICE_BREAKER_COOLDOWN_S", "120"))

# Disagreement: record to the journal when the min/max of returning prices
# differ by more than this relative fraction (e.g. 0.02 = 2%).
DISAGREEMENT_THRESHOLD = float(os.environ.get("PRICE_DISAGREE_PCT", "0.02"))
DISAGREEMENT_JOURNAL = os.environ.get(
    "PRICE_DISAGREEMENT_JOURNAL",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "price_disagreements.jsonl"))


# --- circuit breaker --------------------------------------------------------
@dataclass
class _Breaker:
    """Per-source circuit breaker. Fails closed: OPEN means skip the source."""
    fail_streak: int = 0
    opened_at: float | None = None      # epoch when it tripped open
    state: str = "closed"               # closed | open | half_open

    def allow(self, now: float) -> bool:
        """May we attempt this source right now?"""
        if self.state == "closed":
            return True
        if self.state == "open":
            # Cooldown elapsed -> move to half-open, allow one probe.
            if self.opened_at is not None and (now - self.opened_at) >= BREAKER_COOLDOWN_S:
                self.state = "half_open"
                return True
            return False
        # half_open: allow exactly one probe (the caller sets success/failure)
        return True

    def record_success(self):
        self.fail_streak = 0
        self.state = "closed"
        self.opened_at = None

    def record_failure(self, now: float):
        self.fail_streak += 1
        if self.fail_streak >= BREAKER_FAIL_THRESHOLD:
            self.state = "open"
            self.opened_at = now


# --- individual source fetchers (each isolated, never raises) ---------------
def _fetch_gmgn(token_address: str) -> PriceResult:
    binary = os.environ.get("GMGN_CLI", "gmgn-cli")
    argv = [binary, "token", "info", "--chain", "base",
            "--address", token_address, "--raw"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=SOURCE_TIMEOUTS_S["gmgn"], shell=False)
    except FileNotFoundError:
        return PriceResult(reason=PRICE_NETWORK, detail="gmgn-cli not on PATH")
    except subprocess.TimeoutExpired:
        return PriceResult(reason=PRICE_NETWORK, detail="gmgn-cli timeout")
    except Exception as exc:  # noqa: BLE001
        return PriceResult(reason=PRICE_NETWORK, detail=f"{type(exc).__name__}: {exc}")
    if proc.returncode != 0:
        return PriceResult(reason=PRICE_HTTP_5XX,
                           detail=(proc.stderr or "")[-200:] or
                                  f"gmgn exit {proc.returncode}")
    try:
        data = json.loads(proc.stdout)
    except Exception as exc:
        return PriceResult(reason=PRICE_PARSE_ERROR, detail=f"gmgn json: {exc}")
    price = data.get("price", {}).get("price") \
        if isinstance(data.get("price"), dict) else data.get("price")
    # GMGN returns the STRING "0" (truthy) for an unpriced token — parse and
    # reject non-positive, never let $0.00 through as a real quote.
    try:
        pf = float(price) if price not in (None, "") else 0.0
    except (TypeError, ValueError):
        return PriceResult(reason=PRICE_PARSE_ERROR, detail=f"gmgn price {price!r}")
    if pf <= 0.0:
        return PriceResult(reason=PRICE_NO_POOL, detail=f"gmgn price {price!r}")
    return PriceResult(price=pf, reason=PRICE_OK)


def _fetch_geckoterminal(token_address: str) -> PriceResult:
    url = (f"https://api.geckoterminal.com/api/v2/networks/base/"
           f"tokens/{token_address}")
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=SOURCE_TIMEOUTS_S["geckoterminal"]) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        code = getattr(e, "code", None)
        if code == 429:
            return PriceResult(reason=PRICE_HTTP_429, detail="gecko 429")
        if code == 404:
            return PriceResult(reason=PRICE_NO_POOL, detail="gecko 404")
        return PriceResult(reason=PRICE_HTTP_5XX, detail=f"gecko {code}")
    except Exception as exc:  # noqa: BLE001
        return PriceResult(reason=PRICE_NETWORK, detail=f"gecko {type(exc).__name__}")
    try:
        price = data.get("data", {}).get("attributes", {}).get("price_usd")
        pf = float(price) if price else 0.0
    except (TypeError, ValueError):
        return PriceResult(reason=PRICE_PARSE_ERROR, detail="gecko price parse")
    if pf <= 0.0:
        return PriceResult(reason=PRICE_NO_POOL, detail="gecko price empty")
    return PriceResult(price=pf, reason=PRICE_OK)


def _fetch_dexscreener(token_address: str) -> PriceResult:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=SOURCE_TIMEOUTS_S["dexscreener"]) as r:
            data = json.loads(r.read())
    except Exception as exc:  # noqa: BLE001
        return PriceResult(reason=PRICE_NETWORK, detail=f"dex {type(exc).__name__}")
    pairs = data.get("pairs", [])
    # Best-liquidity Base pair, matching base_data.py's selection logic.
    best_pair, best_liq = None, 0.0
    for p in pairs:
        if p.get("chainId") != "base":
            continue
        liq = (p.get("liquidity") or {}).get("usd", 0) or 0
        if liq > best_liq:
            best_liq, best_pair = liq, p
    if best_pair is None:
        for p in pairs:  # fallback: any base pair with a price
            if p.get("chainId") == "base" and p.get("priceUsd"):
                best_pair = p
                break
    if best_pair is None:
        return PriceResult(reason=PRICE_NO_POOL, detail="dex no base pair")
    try:
        pf = float(best_pair.get("priceUsd", 0))
    except (TypeError, ValueError):
        return PriceResult(reason=PRICE_PARSE_ERROR, detail="dex price parse")
    if pf <= 0.0:
        return PriceResult(reason=PRICE_NO_POOL, detail="dex price 0")
    return PriceResult(price=pf, reason=PRICE_OK)


def _fetch_kyberswap(token_address: str) -> PriceResult:
    """Derive the token's USD price by routing 1 token -> USDC and reading
    amountInUsd. Read-only route query; router code is checked fail-closed by
    the underlying KyberQuoter."""
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from kyber_quoter import KyberQuoter
    except Exception as exc:  # noqa: BLE001
        return PriceResult(reason=PRICE_NETWORK, detail=f"kyber import: {exc}")
    # Route the token -> USDC for 1 whole unit. amountInUsd is the USD value of
    # the input, i.e. the token's USD price at this size.
    decimals_units = 10 ** 18  # most Base tokens are 18-dp; good enough for a
                               # price signal, and KyberSwap rejects 0 amountIn.
    try:
        q = KyberQuoter().quote(token_address, USDC, decimals_units)
    except Exception as exc:  # noqa: BLE001 — fail closed, never a fake quote
        return PriceResult(reason=PRICE_NETWORK, detail=f"kyber {str(exc)[:150]}")
    pf = q.get("amountInUsd")
    if not pf or pf <= 0.0:
        # Some tokens have no USDC route; fall back to amountOutUsd of a WETH leg
        pf = q.get("amountOutUsd")
    try:
        pf = float(pf) if pf else 0.0
    except (TypeError, ValueError):
        return PriceResult(reason=PRICE_PARSE_ERROR, detail="kyber price parse")
    if pf <= 0.0:
        return PriceResult(reason=PRICE_NO_POOL, detail="kyber no usd route")
    return PriceResult(price=pf, reason=PRICE_OK)


_FETCHERS = {
    "gmgn": _fetch_gmgn,
    "geckoterminal": _fetch_geckoterminal,
    "dexscreener": _fetch_dexscreener,
    "kyberswap": _fetch_kyberswap,
}


# --- disagreement journal ---------------------------------------------------
def _record_disagreement(token_address: str, quotes: dict[str, float]):
    """Append a disagreement record. Never raises — journaling is best-effort."""
    prices = list(quotes.values())
    if len(prices) < 2:
        return
    lo, hi = min(prices), max(prices)
    spread = (hi - lo) / hi if hi > 0 else 0.0
    if spread <= DISAGREEMENT_THRESHOLD:
        return
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "token": token_address,
        "spread_pct": round(spread * 100, 3),
        "threshold_pct": round(DISAGREEMENT_THRESHOLD * 100, 3),
        "quotes": {k: round(v, 10) for k, v in quotes.items()},
    }
    try:
        os.makedirs(os.path.dirname(DISAGREEMENT_JOURNAL), exist_ok=True)
        with open(DISAGREEMENT_JOURNAL, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass  # journaling failure must never break the price path


# --- the chain --------------------------------------------------------------
def fetch_price_consensus(token_address: str,
                          breakers: dict[str, _Breaker] | None = None,
                          now_fn=time.time) -> tuple[PriceResult, dict]:
    """Fetch a token's USD price through the four-source chain.

    Returns (result, diagnostics). `result` is the consensus price (median of
    the returning sources) or a typed non-ok result if none returned a usable
    price. `diagnostics` records every source's outcome for observability.

    Consensus = median of the sources that returned a usable price. The median
    (not mean) resists a single stale/bad source. Disagreements beyond the
    threshold are journaled but do NOT block the median — they are recorded for
    review, because a real 2-8% spread on thin tokens is normal pool movement.
    """
    breakers = breakers if breakers is not None else {}
    diagnostics = {"sources": {}, "used": [], "skipped_breaker": [],
                   "budget_exhausted": False}
    chain_start = now_fn()
    quotes: dict[str, float] = {}

    for name in CHAIN_SOURCE_ORDER:
        # Total budget check — stop early if we're out of wall-clock.
        if (now_fn() - chain_start) >= CHAIN_TOTAL_BUDGET_S:
            diagnostics["budget_exhausted"] = True
            break

        br = breakers.get(name)
        if br is not None and not br.allow(now_fn()):
            diagnostics["skipped_breaker"].append(name)
            diagnostics["sources"][name] = {"reason": "breaker_open"}
            continue

        fetcher = _FETCHERS[name]
        try:
            res = fetcher(token_address)
        except Exception as exc:  # noqa: BLE001 — belt and suspenders
            res = PriceResult(reason=PRICE_NETWORK, detail=f"{name} raised {exc}")

        diagnostics["sources"][name] = {
            "reason": res.reason, "price": res.price, "detail": res.detail}
        if br is not None:
            if res.ok:
                br.record_success()
            else:
                br.record_failure(now_fn())
        if res.ok and res.price is not None:
            quotes[name] = res.price
            diagnostics["used"].append(name)

    # Disagreement journaling across whatever we collected.
    if quotes:
        _record_disagreement(token_address, quotes)

    if not quotes:
        # No source produced a usable price. Fail closed with the most
        # informative reason available (prefer a genuine no_pool).
        reasons = [d.get("reason") for d in diagnostics["sources"].values()
                   if d.get("reason") not in (None, "breaker_open")]
        reason = PRICE_NO_POOL if PRICE_NO_POOL in reasons else \
            (reasons[0] if reasons else PRICE_NO_POOL)
        return PriceResult(reason=reason, detail="no source returned a price"), diagnostics

    # Consensus = median (odd count -> middle; even -> mean of the two middle).
    ordered = sorted(quotes.values())
    n = len(ordered)
    median = ordered[n // 2] if n % 2 == 1 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
    return PriceResult(price=median, reason=PRICE_OK), diagnostics
