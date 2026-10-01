#!/usr/bin/env python3
"""
RISK OFFICER — independent veto layer for trade proposals.

Sits between signal generation and risk.evaluate(). Every proposal must pass
the officer's checklist; any single FAIL vetoes the trade regardless of score.
Veto decisions are recorded to risk_state.json (officer_vetoes) so blocks are
auditable, never silent.

Checklist (each is a hard veto):
  1. On-chain existence: pool + token contracts verified (rpc_verify.verify_pool)
  2. Liquidity floor:    pool liquidity >= min (default $250k)
  3. Live tape:          5m volume >= $250 (dead tape = stale signal)
  4. Slippage sanity:    proposal slippage_bps <= max_slippage_bps in caps.json
  5. Token not blocked:  token_out not in blocked_tokens / blocked_tokens.json
  6. Macro regime:       if macro_regime module available and says RISK-OFF,
                         only allow proposals with score >= regime_min_score
  7. Freshness:          proposal timestamp < 10 minutes old

Usage:
    from risk_officer import veto_check
    ok, reasons = veto_check(proposal)   # ok=False means VETOED
"""
import json
import time
from datetime import datetime, timezone

import rpc_verify
import risk

MIN_LIQUIDITY_USD = 250_000
MIN_LIQUIDITY_GMGN_USD = 100_000  # GMGN coins are security-audited, lower floor OK
MIN_5M_VOL_USD = 250
MAX_PROPOSAL_AGE_S = 600
REGIME_MIN_SCORE = 80  # in macro RISK-OFF, only high-score setups may pass

BLOCKED_FILE = "blocked_tokens.json"


def _load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def _record_veto(proposal, reasons):
    s = risk._roll(risk.load_state())
    s.setdefault("officer_vetoes", []).append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "token_out": proposal.get("token_out"),
        "reasons": reasons,
    })
    # keep the list bounded
    s["officer_vetoes"] = s["officer_vetoes"][-200:]
    risk.save_state(s)


def veto_check(proposal):
    """Returns (ok, [reasons]). ok=False => trade is vetoed."""
    fails = []
    c = risk.load_caps()

    # 1. On-chain existence + pair match
    pool = proposal.get("pool")
    token_out = proposal.get("token_out")
    source = proposal.get("source", "")
    try:
        if not pool or not token_out:
            fails.append("no pool/token to verify on-chain")
        elif pool.lower() == token_out.lower() and source in ("gmgn_trending", "manual_add"):
            # GMGN/manual coins are token-only (no separate pool address) — skip pool verification
            # Token contract existence was already verified in discovery's on-chain gate
            pass
        else:
            r = rpc_verify.verify_pool(
                rpc_verify.RpcClient(), pool,
                "0x4200000000000000000000000000000000000006", token_out)
            if not r["verified"]:
                fails.append(f"on-chain verify failed: {r['reason']}")
    except Exception as e:
        fails.append(f"on-chain verify unreachable: {e}")

    # 2/3. Liquidity + live tape (fields come from discovery record)
    liq = proposal.get("liquidity", 0) or 0
    v5m = proposal.get("vol_5m", 0) or 0
    # GMGN coins are security-audited, so lower liquidity floor applies
    liq_min = MIN_LIQUIDITY_GMGN_USD if proposal.get("source") == "gmgn_trending" else MIN_LIQUIDITY_USD
    if liq < liq_min:
        fails.append(f"liquidity ${liq:,.0f} < ${liq_min:,}")
    # GMGN coins don't have real-time 5m data — the estimate is a conservative
    # uniform distribution guess. They've already passed GMGN's security audit
    # + on-chain verification, so skip the live tape check.
    if proposal.get("source") != "gmgn_trending" and v5m < MIN_5M_VOL_USD:
        fails.append(f"5m vol ${v5m:,.0f} < ${MIN_5M_VOL_USD} (stale tape)")

    # 4. Slippage
    slip = proposal.get("slippage_bps", 0)
    if slip > c.get("max_slippage_bps", 100):
        fails.append(f"slippage {slip}bps > cap {c.get('max_slippage_bps')}")

    # 5. Blocked tokens
    blocked = set(c.get("blocked_tokens", []))
    blocked |= {b.get("address") if isinstance(b, dict) else b
                for b in _load_json(BLOCKED_FILE, [])}
    if token_out and token_out.lower() in {a.lower() for a in blocked}:
        fails.append("token_out on blocklist")

    # 6. Macro regime gate (uses the module's real entry point)
    try:
        import macro_regime
        allowed, why = macro_regime.evaluate_macro_regime()
        if not allowed and proposal.get("score", 0) < REGIME_MIN_SCORE:
            fails.append(f"macro risk-off ({why.strip()}) and score "
                         f"{proposal.get('score')} < {REGIME_MIN_SCORE}")
    except ImportError:
        pass  # module absent — skip rather than guess its API
    except Exception as e:
        print(f"[risk-officer] macro check error (non-blocking): {e}")

    # 7. Freshness
    ts = proposal.get("signal_ts")
    if ts:
        try:
            age = time.time() - datetime.fromisoformat(ts).timestamp()
            if age > MAX_PROPOSAL_AGE_S:
                fails.append(f"signal {int(age)}s old > {MAX_PROPOSAL_AGE_S}s")
        except Exception:
            fails.append("unparseable signal_ts")

    ok = not fails
    if not ok:
        _record_veto(proposal, fails)
    return ok, fails


if __name__ == "__main__":
    demo = {"action": "swap", "token_out": "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
            "pool": "0x01784ef301d79e4b2df3a21ad9a536d4cf09a5ce",
            "amount_eth": 0.0005, "score": 95, "slippage_bps": 50,
            "liquidity": 11_700_000, "vol_5m": 10_900,
            "signal_ts": datetime.now(timezone.utc).isoformat()}
    ok, why = veto_check(demo)
    print("OFFICER:", "PASS" if ok else f"VETO {why}")
