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
`UnscreeableField`, and the caller converts that to a blocked verdict.
"""
from __future__ import annotations

import math
from typing import Any, NamedTuple


class UnscreeableField(NamedTuple):
    """A field that could not be parsed. Carries what went wrong."""
    name: str
    raw: Any
    reason: str

    def as_dict(self) -> dict:
        return {"field": self.name, "raw_value": self.raw,
                "reason": self.reason}


class UnscreeableError(Exception):
    """Raised when a security field cannot be safely interpreted.

    Carries one or more UnscreeableField records so the journal line can
    name the offending field and its raw value — WO #1 scope 5.
    """

    def __init__(self, fields: list[UnscreeableField], check: str = "") -> None:
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
    """Parse an external numeric field, or raise UnscreeableError.

    Rejects, per WO #1 scope 2: missing key (caller passes None), None,
    null, empty string, whitespace-only, and malformed numerics like
    'abc' or '12.34.56'. Also rejects NaN/Inf, which compare
    unpredictably against thresholds and can silently pass a `> 10` gate.
    """
    if value is None:
        raise UnscreeableError([UnscreeableField(name, value, "missing/null")],
                               check=name)
    if isinstance(value, bool):
        # bool is an int subclass; a flag where a number belongs is a
        # schema mismatch, not a tax rate of 0 or 1.
        raise UnscreeableError([UnscreeableField(name, value, "bool not numeric")],
                               check=name)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() in _UNKNOWN_TOKENS:
            raise UnscreeableError(
                [UnscreeableField(name, value, "empty/unknown token")],
                check=name)
        try:
            out = float(stripped)
        except ValueError:
            raise UnscreeableError(
                [UnscreeableField(name, value, "malformed numeric")],
                check=name) from None
    elif isinstance(value, (int, float)):
        out = float(value)
    else:
        raise UnscreeableError(
            [UnscreeableField(name, value, f"unsupported type "
                                           f"{type(value).__name__}")],
            check=name)

    if math.isnan(out) or math.isinf(out):
        raise UnscreeableError([UnscreeableField(name, value, "nan/inf")],
                               check=name)
    return out


def parse_fields(source: dict, spec: dict[str, str],
                 check: str = "") -> dict[str, float]:
    """Parse a named set of numeric fields, collecting ALL failures.

    Raises once, with every offending field listed, rather than failing
    on the first — a journal line naming three bad fields is more useful
    than three lines naming one.
    """
    bad: list[UnscreeableField] = []
    out: dict[str, float] = {}
    for name, key in spec.items():
        try:
            out[name] = parse_num(source.get(key), key)
        except UnscreeableError as exc:
            bad.extend(exc.fields)
    if bad:
        raise UnscreeableError(bad, check=check)
    return out


#: GoPlus security fields that are NUMERIC and gate the verdict.
#: Each must parse or the token is UNSCREENABLE.
GOPLUS_NUMERIC_FIELDS = {
    "buy_tax": "buy_tax",
    "sell_tax": "sell_tax",
    "top10_holder_concentration": "top10_holder_concentration",
}
