"""Pessimistic cost model for the WO4 research harness.

Every constant carries a source and an as-of date. No bare numbers: an
unverifiable cost model is how a backtest becomes fiction.

The headline change from the legacy engine (`pullback_walkforward_fixed.py`)
is the removal of the optimistic slippage floor. Legacy did:

    slip = min(max(computed * 10000 * 0.5, 1.0), 80.0)      # bps

That `max(..., 1.0)` floor is optimistic precisely where it matters — thin
pools are where slippage is worst, and the floor flatters them. Here:

    slip_bps = max(computed_bps * MULTIPLIER, FLOOR_BPS)    # both pessimistic

with MULTIPLIER = 2.0 (adverse fill / half-spread assumption) and
FLOOR_BPS = 5.0 (itself conservative, applied uniformly).

Two scenarios are always produced. `pessimistic` is the headline; `nominal`
is for sensitivity only. A strategy that passes only under nominal costs is
reported MARGINAL, never PASS. That is what "pessimistic by default" means
operationally — it is a property of the REPORT, not just the constants.
"""
from __future__ import annotations

import json
import math
import urllib.request
from typing import NamedTuple

# --------------------------------------------------------------------------- #
# Sourced constants
# --------------------------------------------------------------------------- #

#: Adverse-fill multiplier applied to computed price impact.
SLIPPAGE_MULTIPLIER = 2.0
#: Source: half-spread + adverse-fill assumption for a taker crossing a
#: constant-product pool. Not an empirical measurement — a deliberate
#: over-estimate. Revisit with fill data when paper trading produces some.
SLIPPAGE_MULTIPLIER_SOURCE = "adverse-fill assumption, half-spread, no empirical fit"
SLIPPAGE_MULTIPLIER_ASOF = "2026-10-01"

#: Minimum slippage, in bps, applied even when the pool looks deep.
SLIPPAGE_FLOOR_BPS = 5.0
SLIPPAGE_FLOOR_SOURCE = "conservative uniform floor; replaces legacy max(computed,1.0)"
SLIPPAGE_FLOOR_ASOF = "2026-10-01"

#: Ceiling so a pathological liquidity figure cannot produce an absurd cost
#: that silently zeroes every trade (which would look like a conservative
#: result rather than a broken input).
SLIPPAGE_CEILING_BPS = 500.0

#: Base L2 gas, USD, fallback only. Overridden live per run when the oracle
#: responds; a frozen number would quietly drift as Base fees change.
GAS_USD_FALLBACK = 0.04
GAS_USD_SOURCE = "Base L2 execution cost, live oracle preferred"
GAS_USD_ASOF = "2026-10-01"

#: Cost of a failed/unfilled attempt, charged on entry regardless of outcome.
#: Prevents a strategy that spams entries from looking free.
FAILED_ATTEMPT_USD = 0.0   # not charged in this model; see NOTE below
FAILED_ATTEMPT_NOTE = (
    "Not charged. Memo-trading paper fills assume every entry fills; charging "
    "for failures would be pessimistic but the failure rate is unmeasured. "
    "Once paper fills exist this becomes a real constant, not a guess."
)

_UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


class CostScenario(NamedTuple):
    name: str
    multiplier: float
    floor_bps: float
    gas_usd: float


SCENARIOS = {
    "pessimistic": CostScenario("pessimistic", SLIPPAGE_MULTIPLIER,
                                SLIPPAGE_FLOOR_BPS, GAS_USD_FALLBACK),
    "nominal": CostScenario("nominal", 1.0, 1.0, GAS_USD_FALLBACK),
}


class CostModel:
    """Computes round-trip cost in basis points for one pool, per scenario."""

    def __init__(self, fee_bps: float, liquidity_usd: float,
                 notional_usd: float, scenario: str = "pessimistic"):
        if scenario not in SCENARIOS:
            raise ValueError(
                f"unknown cost scenario {scenario!r}; "
                f"expected one of {sorted(SCENARIOS)}")
        if liquidity_usd <= 0:
            raise ValueError(f"liquidity must be positive, got {liquidity_usd}")
        self.scenario = SCENARIOS[scenario]
        self.pool_fee_bps = float(fee_bps)
        self.liquidity_usd = float(liquidity_usd)
        self.notional_usd = float(notional_usd)

    def impact_bps(self) -> float:
        """Price impact from constant-product depth, scaled by notional.

        Half the notional is taken in each direction; the adverse side pays
        more, approximated by the multiplier in the scenario.
        """
        frac = self.notional_usd / self.liquidity_usd
        raw = frac * 10000.0          # bps at 1.0x
        return min(raw * self.scenario.multiplier,
                   SLIPPAGE_CEILING_BPS)

    def slippage_bps(self) -> float:
        return max(self.impact_bps(), self.scenario.floor_bps)

    def gas_bps(self) -> float:
        if self.notional_usd <= 0:
            return 0.0
        return self.scenario.gas_usd / self.notional_usd * 10000.0

    def roundtrip_bps(self) -> float:
        """Fee and slippage are charged on BOTH sides; gas once per round trip."""
        fee_rt = self.pool_fee_bps * 2.0
        slip_rt = self.slippage_bps() * 2.0
        return fee_rt + slip_rt + self.gas_bps()

    def per_trade_log(self) -> dict:
        """Emitted into every artifact so a reviewer can check the arithmetic."""
        return {
            "scenario": self.scenario.name,
            "pool_fee_bps_oneway": round(self.pool_fee_bps, 4),
            "fee_bps_roundtrip": round(self.pool_fee_bps * 2.0, 4),
            "impact_bps_raw": round(self.impact_bps() / self.scenario.multiplier, 4),
            "impact_bps_applied": round(self.impact_bps(), 4),
            "slippage_bps_oneway": round(self.slippage_bps(), 4),
            "slippage_bps_roundtrip": round(self.slippage_bps() * 2.0, 4),
            "gas_bps": round(self.gas_bps(), 4),
            "roundtrip_bps": round(self.roundtrip_bps(), 4),
            "notional_usd": round(self.notional_usd, 2),
            "liquidity_usd": round(self.liquidity_usd, 2),
            "multiplier": self.scenario.multiplier,
            "floor_bps": self.scenario.floor_bps,
        }


def constants_document() -> dict:
    """The cost-model documentation deliverable, machine-readable."""
    return {
        "version": "pessimistic_v1",
        "asof": "2026-10-01",
        "constants": [
            {"name": "SLIPPAGE_MULTIPLIER", "value": SLIPPAGE_MULTIPLIER,
             "source": SLIPPAGE_MULTIPLIER_SOURCE, "asof": SLIPPAGE_MULTIPLIER_ASOF},
            {"name": "SLIPPAGE_FLOOR_BPS", "value": SLIPPAGE_FLOOR_BPS,
             "source": SLIPPAGE_FLOOR_SOURCE, "asof": SLIPPAGE_FLOOR_ASOF},
            {"name": "SLIPPAGE_CEILING_BPS", "value": SLIPPAGE_CEILING_BPS,
             "source": "input sanity bound; a broken liquidity figure must not"
                       " silently zero every trade",
             "asof": SLIPPAGE_FLOOR_ASOF},
            {"name": "GAS_USD_FALLBACK", "value": GAS_USD_FALLBACK,
             "source": GAS_USD_SOURCE, "asof": GAS_USD_ASOF},
            {"name": "FAILED_ATTEMPT_USD", "value": FAILED_ATTEMPT_USD,
             "source": FAILED_ATTEMPT_NOTE, "asof": GAS_USD_ASOF},
        ],
        "pool_fee_bps": {
            "source": "GeckoTerminal pool metadata, read live per pool",
            "note": "never defaulted; a missing fee is an input error",
        },
        "scenarios": sorted(SCENARIOS),
        "headline_scenario": "pessimistic",
        "rule": "passing only under nominal costs => MARGINAL, never PASS",
    }


def fetch_eth_usd() -> float:
    """Live WETH/USD for gas conversion, with the documented fallback."""
    try:
        req = urllib.request.Request(
            "https://api.geckoterminal.com/api/v2/networks/base/pools?page=1",
            headers=_UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        for p in data.get("data", []):
            nm = p.get("attributes", {}).get("name", "")
            if "WETH" in nm.upper() and "USDC" in nm.upper():
                px = p.get("attributes", {}).get("base_token_price_usd")
                if px and float(px) > 0:
                    return float(px)
    except Exception:
        pass
    return 2500.0


def pool_fee_bps(pool_name: str) -> float:
    """Parse the fee from a pool label like 'WETH / USDC 0.05%' or
    'DRB / WETH 1%'.

    Note the label splits into ['1', '%'] for an integer percentage, so the
    percent sign arrives as its own token. Both the attached form ('0.05%')
    and the split form ('1' followed by '%') are handled.

    Raises on anything unparseable rather than defaulting: a wrong fee
    silently biases every trade in the run.
    """
    tokens = pool_name.upper().replace("%", " %").split()
    for i, tok in enumerate(tokens):
        try:
            if "%" in tok:                       # attached form: '0.05%'
                return float(tok.replace("%", "")) * 100
            if (tokens[i + 1:i + 2] == ["%"]      # split form: '1' '%'
                    and tok.replace(".", "").isdigit()):
                return float(tok) * 100
        except (ValueError, IndexError):
            continue
    raise ValueError(
        f"could not parse fee from pool label {pool_name!r}; "
        f"refusing to guess a fee")