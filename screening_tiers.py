#!/usr/bin/env python3
"""
screening_tiers.py — Tiered signal intake screening for Base (adapted from GMGN Part 1 §3.1).

McKenzie, 2026-10-07: "Proceed with the WO's stated thresholds ($20K/$50K/$100K mcap floors,
85%/50% concentration, 40% new-wallet). Tuning happens against paper data, not upfront."

Stage definitions (by pair age = now - pairCreatedAt):
  FRESH    : pair age < 24h
  ESTABLISHING: pair age 1-7d
  ESTABLISHED : pair age > 7d

Gate logic:
  Run stage-appropriate gates on an already-fetched bundle.
  Cache per address, 24h TTL (outside this module's scope).
  Any source error → mark sub-gate "unknown", fall back to resolved gates, log miss.
  Never fail the whole intake run on a source error.
  Fail-open with logging; fail-closed only on positive bad signals (REJECT gates).

Bundle structure expected:
{
  "dexscreener": {
    "pairCreatedAt": int,      # epoch ms
    "liquidity_usd": float,
    "volume_24h": int,    # txn COUNT (buys+sells), not USD volume
    "txns_24h": {"buys": int, "sells": int},
    "mcap": float,
  },
  "blockscout": {
    "holders": [{"address": str, "balance": str, "pct": float}],
    "holder_count": int,
    "total_supply": str,       # WHOLE tokens if from Blockscout human-readable; see total_supply_raw
    "total_supply_raw": str,   # RAW base units — required for concentration
  },
  "goplus": {
    "dex": [{"pair": str, "pool_manager": str}],  # pool exclusion list
    "total_supply": str,       # WHOLE tokens (GoPlus unit)
    "holder_count": int,
  }
}

UNITS NOTE (verified live on BOAR, 2026-10-07): GoPlus `total_supply`
(100000000000) and Blockscout holder `value` (1.77e28) are NOT the same
unit — the former is whole tokens, the latter raw 18-decimal base units.
Concentration math must divide raw holder values by the RAW supply
(Blockscout `total_supply`), or every ratio is ~1e19%.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


# =============================================================================
# Safe numeric coercion
# =============================================================================

def _safe_float(value, default: float = 0.0) -> float:
    """Coerce an external value to float, tolerating None/""/bad types.

    Unlike safe_parse.parse_num (which fails CLOSED for security fields),
    tier-screening market data is not a safety signal — a missing mcap is
    handled by the mcap floor gate, not by making the token UNSCREENABLE.
    The WO is explicit: source errors mark a sub-gate "unknown" and the
    run continues on the gates that did resolve.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else default
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return default
        try:
            f = float(s)
            return f if math.isfinite(f) else default
        except ValueError:
            return default
    return default


# =============================================================================
# Stage Classification
# =============================================================================

def classify_stage(pair_created_at_ms: int | None) -> str:
    """Classify a token by pair age.

    Returns: "FRESH" (< 1 day), "ESTABLISHING" (1-7d), "ESTABLISHED" (> 7d),
    or "UNKNOWN" if pair_created_at_ms is missing, invalid, or wrong type.
    
    pair_created_at_ms is epoch milliseconds.
    FRESH = < 24 hours, ESTABLISHING = 1-7 days, ESTABLISHED = > 7 days.
    """
    if not pair_created_at_ms:
        return "UNKNOWN"
    # Type guard: a non-numeric pairCreatedAt (str from a bad source)
    # would raise TypeError on the comparison below. Treat as UNKNOWN.
    try:
        if int(pair_created_at_ms) <= 0:
            return "UNKNOWN"
    except (ValueError, TypeError):
        return "UNKNOWN"

    # pairCreatedAt is epoch ms; convert to age in days
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    age_ms = now_ms - int(pair_created_at_ms)
    age_days = age_ms / 86_400_000.0  # 86.4 million ms per day

    if age_days < 1:  # < 24h = FRESH
        return "FRESH"
    elif age_days < 7:
        return "ESTABLISHING"
    else:
        return "ESTABLISHED"


# =============================================================================
# Concentration Math
# =============================================================================

BURN_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x0000000000000000000000000000000000000001",
    "0x000000000000000000000000000000000000dead",
    "0x000000000000000000000000000000000000DEAD",
}


def _is_pool_holder(addr: str, pool_addresses: list[str]) -> bool:
    """Check if an address is a known pool/pair address."""
    addr_lower = addr.lower()
    return any(p.lower() == addr_lower for p in pool_addresses) if pool_addresses else False


def pool_exclusion_set(goplus_dex: list[dict] | None) -> list[str]:
    """Build the pool-exclusion list from GoPlus's `dex[]` array.

    Two shapes must both be handled (verified live on BOAR, 2026-10-07):

      * UniV3 / standard DEXs — `dex[].pair` IS the pool contract address.
      * UniV4 — `dex[].pair` is a 32-byte POOL ID, not an address; the
        address that actually appears in `holders[]` is `dex[].pool_manager`
        (BOAR's top holder is 0x498581fF…b2b, the UniV4 PoolManager).

    Excluding only `pair` silently fails on UniV4, so the pool is counted
    as a holder and every UniV4 token looks concentrated. Include both.
    """
    out: list[str] = []
    seen: set[str] = set()
    for d in goplus_dex or []:
        for key in ("pair", "pool_manager"):
            v = (d or {}).get(key)
            if not v or not isinstance(v, str):
                continue
            # Only 20-byte addresses can appear in holders[]; a 32-byte
            # pool id (len 66 with 0x) never matches a holder address.
            if len(v) == 42:
                a = v.lower()
                if a not in seen:
                    seen.add(a)
                    out.append(a)
    return out


def concentration_top10(
    holders: list[dict],
    total_supply: str | int | float,
    exclusions: list[str] | None = None,
    supply_raw: str | int | float | None = None,
) -> float | None:
    """Calculate top-10 holder concentration percentage.

    Holders with balances in burn addresses or that are among the provided
    pool/pair addresses are EXCLUDED from the calculation (the LP pool
    dominates most token holders and must not count).

    UNITS (verified live, BOAR, 2026-10-07): Blockscout holder `value` is
    a RAW base-unit integer (18 decimals), while GoPlus `total_supply` is
    WHOLE TOKENS. Dividing one by the other yields ~1.8e19% — a silent
    unit mismatch that makes the gate meaningless.

    `supply_raw` is the raw-unit supply (Blockscout `total_supply`), which
    is the only denominator commensurate with raw holder values. The WO's
    "total_supply from GoPlus" is kept as `total_supply` for whole-token
    callers, but when raw holder values are supplied the raw denominator
    is REQUIRED. If it is missing, the calculation returns None (an
    `unknown` sub-gate) rather than emitting a nonsense percentage.

    Returns: concentration 0.0-100.0, or None if calculation cannot be done.
    """
    if not holders:
        return None

    # Pick the denominator commensurate with the holder values.
    # Raw holder values (>= 1e12, i.e. a realistic 18-decimal balance) need
    # a raw supply. If only a whole-token supply was provided, we cannot
    # mix them — return None so the caller marks the gate `unknown`.
    denom = supply_raw if supply_raw not in (None, "") else total_supply
    if denom in (None, ""):
        return None
    try:
        total = float(denom)
        if total <= 0 or not math.isfinite(total):
            return None
    except (ValueError, TypeError):
        return None

    # Get pool addresses for exclusion
    pool_addrs = exclusions or []
    pool_set = {p.lower() for p in pool_addrs}

    # Aggregate top-10 by value, excluding pools/burns
    holder_values: list[dict] = []
    for h in holders[:50]:  # top 50 gives us top 10 buffer
        addr = h.get("address", "")
        if not addr:
            continue

        # Skip burn addresses
        if addr.lower() in BURN_ADDRESSES:
            continue

        # Skip pool addresses
        if addr.lower() in pool_set:
            continue

        # Parse balance (raw base units from Blockscout `value`)
        bal_raw = h.get("balance", h.get("value", 0))
        try:
            bal = float(bal_raw) if bal_raw else 0
            if bal <= 0 or not math.isfinite(bal):
                continue
        except (ValueError, TypeError):
            continue

        holder_values.append({"address": addr, "balance": bal})

    if len(holder_values) < 1:
        return None

    # Sort by balance desc, take top 10
    holder_values.sort(key=lambda x: x["balance"], reverse=True)
    top10 = holder_values[:10]

    top10_total = sum(h["balance"] for h in top10)
    concentration = (top10_total / total) * 100.0

    # A ratio above 100% is arithmetically impossible for a real holder
    # set and means the units disagree. Report `unknown`, never a number.
    if not (0 <= concentration <= 100):
        return None
    return concentration


# =============================================================================
# New-Wallet Suspicion Analysis
# =============================================================================

def estimate_new_wallet_ratio(
    holders: list[dict],
    account_ages: dict[str, int],
    threshold_days: int = 7,
    sample_size: int = 20,
) -> float | None:
    """Estimate the proportion of holders whose wallets are younger than threshold_days.

    `account_ages` maps holder address → wallet_age_days.
    In production, this comes from Blockscout's address transaction history
    (oldest transaction timestamp).

    Returns: ratio 0.0-1.0, or None if insufficient data.
    """
    if not holders or not account_ages:
        return None

    sample = holders[:sample_size]
    if not sample:
        return None

    new_count = 0
    for h in sample:
        addr = h.get("address", "").lower()
        if not addr:
            continue
        age = account_ages.get(addr)
        if age is not None and age < threshold_days:
            new_count += 1

    return new_count / len(sample)


# =============================================================================
# Token Screening Function
# =============================================================================

# Threshold constants (per WO spec)
MCAP_FLOOR_FRESH = 20_000         # $20K
MCAP_FLOOR_EST = 50_000           # $50K
MCAP_FLOOR_ESTABLISHED = 100_000  # $100K

CONC_REJECT_THRESHOLD = 85          # percent
CONC_FLAG_THRESHOLD = 50            # percent
NEW_WALLET_FLAG_THRESHOLD = 0.40    # 40%

TXN_MIN_FRESH = 100               # buys + sells in 24h


def screen_token(bundle: dict) -> dict[str, Any]:
    """Run stage-appropriate gates on a token's data bundle.

    Returns: {
      "verdict": "PASS" | "REJECT" | "FLAG",
      "stage": str,
      "reasons": [...],
      "metrics": {...}
    }

    Note: REJECT is fail-closed; FLAG is advisory (proceed with caution).
    The caller (intake layer) decides what FLAG means for its pipeline.
    """
    if not bundle:
        return {
            "verdict": "REJECT",
            "stage": "UNKNOWN",
            "reasons": ["empty bundle"],
            "metrics": {},
        }

    # --- Extract data from bundle ---
    dex = bundle.get("dexscreener", {}) or {}
    block = bundle.get("blockscout", {}) or {}
    goplus = bundle.get("goplus", {}) or {}

    pair_created = dex.get("pairCreatedAt")
    mcap_raw = dex.get("mcap")
    mcap = _safe_float(mcap_raw)
    liq = _safe_float(dex.get("liquidity_usd"))
    txns = dex.get("txns_24h") or {}
    vol_24h = int(_safe_float(txns.get("buys")) + _safe_float(txns.get("sells")))
    # txns_24h is a COUNT (buys + sells), not USD volume. The WO calls
    # this "volume_24h" in the metrics, so we keep the key name but the
    # value is txn count (documented here to avoid the mislabel).
    txn_count = vol_24h

    holders = block.get("holders") or []
    # Two supplies, deliberately distinct (live unit mismatch, BOAR):
    #   total_supply      — whole tokens (GoPlus), human-readable
    #   total_supply_raw  — raw base units (Blockscout), matches holder values
    total_supply = block.get("total_supply") or goplus.get("total_supply") or "0"
    total_supply_raw = block.get("total_supply_raw")
    # Pool exclusion must include dex[].pool_manager as well as dex[].pair:
    # on UniV4 the pair is a 32-byte pool id, and the address that actually
    # holds tokens is the PoolManager (BOAR's top holder).
    pool_addresses = pool_exclusion_set(goplus.get("dex"))
    account_ages: dict[str, int] = bundle.get("_account_ages") or {}

    # --- Determine stage ---
    stage = classify_stage(pair_created)

    result = {
        "verdict": "PASS",
        "stage": stage,
        "reasons": [],
        "unknown": [],   # sub-gates that could not be evaluated (WO: fail-open + log)
        "metrics": {
            "pair_age_hours": None,
            "mcap": mcap,
            "liquidity_usd": liq,
            "volume_24h": vol_24h,  # txn count per WO naming (key kept for WO compat)
            "concentration_top10": None,
            "new_wallet_ratio": None,
        },
    }

    # Calculate age in hours for metrics
    if pair_created:
        try:
            result["metrics"]["pair_age_hours"] = round(
                (datetime.now(timezone.utc).timestamp() * 1000
                 - int(pair_created)) / 3_600_000, 1)
        except (ValueError, TypeError):
            pass  # leave pair_age_hours as None

    # === Stage-specific gates ===
    #
    # Gates are CUMULATIVE per the WO ("all FRESH gates, plus", "all above,
    # plus"). Severity is ranked REJECT > FLAG > PASS: a later advisory
    # annotation must never soften an earlier REJECT. The old code assigned
    # verdict directly at each check, so a missing-socials FLAG silently
    # overwrote a thin-mcap REJECT.
    SEVERITY = {"PASS": 0, "FLAG": 1, "REJECT": 2}

    def _raise_verdict(v: str) -> None:
        """Escalate the verdict; never downgrade."""
        if SEVERITY[v] > SEVERITY[result["verdict"]]:
            result["verdict"] = v

    def _flag(reason: str, tag: str | None = None) -> None:
        _raise_verdict("FLAG")
        result["reasons"].append(reason)
        if tag:
            result["reasons"].append(tag)

    def _reject(reason: str) -> None:
        _raise_verdict("REJECT")
        result["reasons"].append(reason)

    if stage == "UNKNOWN":
        # WO addendum (2026-10-07): unknown stage is a source gap, not a
        # positive bad signal. Fail-open: mark the stage gate `unknown`
        # and let other gates (if any resolved) proceed. REJECT on age
        # alone would drop a $100K whale buy on a metadata gap.
        result["unknown"].append(
            "stage: cannot determine pair age (missing/invalid pairCreatedAt)")
        # Age-based gates can't fire without a stage, but mcap/liq/txn
        # checks below may still resolve from other sources.

    # --- FRESH gates (apply to every stage) ---
    # WO addendum (2026-10-07): missing mcap is a DATA gap, not a bad signal.
    # Fail-open: if DexScreener returned no mcap (rate limit, new pair not
    # yet indexed), mark it `unknown` rather than REJECT. This matches the
    # GoPlus "" = unknown precedent already signed off.
    mcap_known = mcap_raw is not None and mcap > 0
    if not mcap_known:
        result["unknown"].append("mcap: no marketCap from DexScreener")
    else:
        if mcap < MCAP_FLOOR_FRESH:
            _reject(f"mcap ${mcap:,.0f} < ${MCAP_FLOOR_FRESH:,} floor")
    if vol_24h < TXN_MIN_FRESH and txn_count > 0:
        # Only apply if we got a real count; a zero may mean DexScreener
        # didn't index txns yet, not that there are none.
        _reject(f"txns 24h {vol_24h} < {TXN_MIN_FRESH} min")

    socials = len((dex.get("info") or {}).get("socials") or [])
    websites = len((dex.get("info") or {}).get("websites") or [])
    if socials + websites == 0:
        _flag("no verifiable social presence", "flag:no_socials")

    if stage == "FRESH":
        return result

    # --- ESTABLISHING gates (apply to ESTABLISHING and ESTABLISHED) ---
    if mcap_known and mcap < MCAP_FLOOR_EST:
        _reject(f"mcap ${mcap:,.0f} < ${MCAP_FLOOR_EST:,} floor")

    conc = concentration_top10(holders, total_supply, pool_addresses,
                               supply_raw=total_supply_raw)
    result["metrics"]["concentration_top10"] = conc
    if conc is None:
        # WO failure handling: an unresolvable sub-gate is marked `unknown`
        # and the run continues on the gates that did resolve. It is NOT a
        # REJECT — that would fail-closed on a source error, which the WO
        # reserves for positive bad signals only.
        result["unknown"].append(
            "concentration: no usable holders[]/total_supply from "
            "Blockscout/GoPlus")
    else:
        if conc > CONC_REJECT_THRESHOLD:
            _reject(f"top-10 concentration {conc:.1f}% > {CONC_REJECT_THRESHOLD}%")
        elif conc > CONC_FLAG_THRESHOLD:
            _flag(f"top-10 concentration {conc:.1f}% > {CONC_FLAG_THRESHOLD}%",
                  "flag:high_concentration")

    if stage == "ESTABLISHING":
        return result

    # --- ESTABLISHED gates (apply to ESTABLISHED only) ---
    if mcap_known and mcap < MCAP_FLOOR_ESTABLISHED:
        _reject(f"mcap ${mcap:,.0f} < ${MCAP_FLOOR_ESTABLISHED:,} floor")

    new_ratio = estimate_new_wallet_ratio(holders, account_ages,
                                          threshold_days=7, sample_size=20)
    result["metrics"]["new_wallet_ratio"] = new_ratio
    if new_ratio is None:
        # WO: "skip this sub-gate on API errors rather than failing the run."
        # account_ages is populated by the fetch layer from Blockscout's
        # per-address oldest-transaction lookup (up to 20 extra calls).
        result["unknown"].append(
            "new_wallet_ratio: no per-holder account ages available")
    elif new_ratio > NEW_WALLET_FLAG_THRESHOLD:
        _flag(f"new-wallet ratio {new_ratio:.1%} > {NEW_WALLET_FLAG_THRESHOLD:.0%}",
              "flag:new_wallet_suspicious")

    # NOTE (WO scope cut, deferred per McKenzie 2026-10-07):
    # The new-wallet gate logic is complete and unit-tested but NOT wired
    # in production. Wiring requires ~20 Blockscout per-address
    # oldest-transaction lookups per token (the WO's "up to 20 calls").
    # Without account_ages populated, the gate permanently returns `unknown`
    # and pass-through (fail-open per WO failure handling). Deferring to
    # a follow-up once the lane is live, to avoid burning quota on a gate
    # that only produces FLAG annotations.

    return result


# =============================================================================
# Entry Point / CLI
# =============================================================================

if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Screen a token by tier")
    ap.add_argument("bundle_json", help="JSON bundle (DexScreener + Blockscout + GoPlus)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--test-boar", action="store_true", help="Run live BOAR test")
    args = ap.parse_args()

    if args.test_boar:
        # Live BOAR test
        import urllib.request

        addr = "0x0cbf291Ba052174879d90bf781dF1A5F2BC5Bb07"

        # DexScreener
        ds_resp = json.loads(
            urllib.request.urlopen(
                urllib.request.Request(
                    f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
                    headers={"User-Agent": "Mozilla/5.0"},
                ),
                timeout=15,
            ).read()
        )

        # GoPlus
        gp_resp = json.loads(
            urllib.request.urlopen(
                urllib.request.Request(
                    f"https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses={addr}",
                    headers={"User-Agent": "Mozilla/5.0"},
                ),
                timeout=15,
            ).read()
        )
        gp_rec = (gp_resp.get("result") or {}).get(addr.lower(), {})

        bundle = {
            "dexscreener": {
                "pairCreatedAt": ds_resp.get("pairs", [{}])[0].get("pairCreatedAt"),
                "liquidity_usd": ds_resp.get("pairs", [{}])[0].get("liquidity", {}).get("usd", 0),
                "volume_24h": ds_resp.get("pairs", [{}])[0].get("volume", {}).get("h24", 0),
                "mcap": ds_resp.get("pairs", [{}])[0].get("marketCap") or
                        ds_resp.get("pairs", [{}])[0].get("fdv") or 0,
                "txns_24h": ds_resp.get("pairs", [{}])[0].get("txns", {}).get("h24", {"buys": 0, "sells": 0}),
                "info": ds_resp.get("pairs", [{}])[0].get("info", {}),
            },
            "blockscout": {
                "holders": [],  # CLI live test doesn't fetch Blockscout holders
                "total_supply_raw": gp_rec.get("total_supply"),  # raw for concentration
            },
            "goplus": {
                "dex": gp_rec.get("dex", []),
                "total_supply": gp_rec.get("total_supply"),
                "holder_count": gp_rec.get("holder_count"),
            },
        }

        result = screen_token(bundle)
        print(json.dumps(result, indent=2))
        exit(0)

    bundle = json.loads(args.bundle_json)
    result = screen_token(bundle)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Stage: {result['stage']}")
        print(f"Verdict: {result['verdict']}")
        if result["reasons"]:
            print("Reasons:", "; ".join(result["reasons"]))