"""Fail-closed numeric parsing for external security data (WO #1).

McKenzie, 2026-09-30 20:47: "Empty tax fields reject as UNSCREENABLE.
No exceptions, no defaults."

The bug this exists to kill: GoPlus returns `"?"` for a token whose tax
fields it cannot determine. `float("?")` raises, the exception was
caught by a handler that only docked 20 points, and the token was
reported "not honeypot, renounced". A crashed check produced a PASSING
verdict. That is fail-open, and a passing security verdict is the most
expensive kind of wrong answer this stack can emit.

The rule here: a field we cannot parse is NOT zero. It is unknown, and
unknown about a safety field means unscreenable, which means blocked.
`parse_num` never returns a default for a malformed value — it raises
`UnscreenableError` (carrying one or more `UnscreenableField` records), and
the caller converts that to a blocked verdict.
"""
from __future__ import annotations

import math
from typing import Any, NamedTuple


class UnscreenableField(NamedTuple):
    """A field that could not be parsed. Carries what went wrong."""
    name: str
    raw: Any
    reason: str

    def as_dict(self) -> dict:
        return {"field": self.name, "raw_value": self.raw,
                "reason": self.reason}


class UnscreenableError(Exception):
    """Raised when a security field cannot be safely interpreted.

    Carries one or more UnscreenableField records so the journal line can
    name the offending field and its raw value — WO #1 scope 5.
    """

    def __init__(self, fields: list[UnscreenableField], check: str = "") -> None:
        self.fields = list(fields)
        self.check = check
        names = ", ".join(f.name for f in self.fields)
        super().__init__(f"unscreenable: {names or 'unknown field'}")

    def as_dict(self) -> dict:
        return {
            "verdict": "UNSCREENABLE",
            "check": self.check,
            "fields": [f.as_dict() for f in self.fields],
        }


#: Values that are legitimately "we don't know", not "zero".
_UNKNOWN_TOKENS = {"", "?", "n/a", "na", "null", "none", "-", "unknown",
                   "undefined", "not available", "not_available"}


def parse_num(value: Any, name: str) -> float:
    """Parse an external numeric field, or raise UnscreenableError.

    Rejects, per WO #1 scope 2: missing key (caller passes None), None,
    null, empty string, whitespace-only, and malformed numerics like
    'abc' or '12.34.56'. Also rejects NaN/Inf, which compare
    unpredictably against thresholds and can silently pass a `> 10` gate.
    """
    if value is None:
        raise UnscreenableError([UnscreenableField(name, value, "missing/null")],
                               check=name)
    if isinstance(value, bool):
        # bool is an int subclass; a flag where a number belongs is a
        # schema mismatch, not a tax rate of 0 or 1.
        raise UnscreenableError([UnscreenableField(name, value, "bool not numeric")],
                               check=name)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() in _UNKNOWN_TOKENS:
            raise UnscreenableError(
                [UnscreenableField(name, value, "empty/unknown token")],
                check=name)
        try:
            out = float(stripped)
        except ValueError:
            raise UnscreenableError(
                [UnscreenableField(name, value, "malformed numeric")],
                check=name) from None
    elif isinstance(value, (int, float)):
        out = float(value)
    else:
        raise UnscreenableError(
            [UnscreenableField(name, value, f"unsupported type "
                                           f"{type(value).__name__}")],
            check=name)

    if math.isnan(out) or math.isinf(out):
        raise UnscreenableError([UnscreenableField(name, value, "nan/inf")],
                               check=name)
    return out


def parse_fields(source: dict, spec: dict[str, str],
                 check: str = "") -> dict[str, float]:
    """Parse a named set of numeric fields, collecting ALL failures.

    Raises once, with every offending field listed, rather than failing
    on the first — a journal line naming three bad fields is more useful
    than three lines naming one.
    """
    bad: list[UnscreenableField] = []
    out: dict[str, float] = {}
    for name, key in spec.items():
        try:
            out[name] = parse_num(source.get(key), key)
        except UnscreenableError as exc:
            bad.extend(exc.fields)
    if bad:
        raise UnscreenableError(bad, check=check)
    return out


#: GoPlus security fields that are NUMERIC and gate the verdict.
#: Each must parse or the token is UNSCREENABLE.
# GoPlus numeric fields. top10_holder_concentration is NOT a GoPlus key
# (absent 10/10 sampled live tokens — defect #2). Concentration math must
# be derived from the holders[] array in the intake layer, not parsed here.
# Keeping it in this spec caused parse_fields() to raise UnscreenableError
# on every real token, since the missing key resolves to None.
GOPLUS_NUMERIC_FIELDS = {
    "buy_tax": "buy_tax",
    "sell_tax": "sell_tax",
}
