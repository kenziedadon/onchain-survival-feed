"""WO #1 — fail-closed on missing/empty/malformed tax fields.

McKenzie, 2026-09-30 20:47: "Empty tax fields reject as UNSCREENABLE.
No exceptions, no defaults."

The incident: at orchestrator tick 1964, two discoveries (Surplus,
GITLAWB) crashed GoPlus parsing with `could not convert string to
float: ''`. The crash was caught by a handler that docked 20 points and
continued, so the check reported BOTH tokens "not honeypot, renounced".
A crashed check produced a PASSING verdict. GITLAWB happened to be
genuinely sellable — luck, not validation.

Every test here fails on the pre-fix code. Cases 1-3 are McKenzie's
three; 4-6 are the adjacent shapes the WO said to add if the code path
distinguishes them (it does).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from safe_parse import (GOPLUS_NUMERIC_FIELDS, UnscreeableError,
                        UnscreeableField, parse_fields, parse_num)
import token_grade as TG


# ====================================================================== #
# 1-6. Parsing: every unparseable shape raises, never defaults
# ====================================================================== #

def test_case_1_null_tax_field():
    """McKenzie's case 1: a null tax field is unscreenable, not 0."""
    with pytest.raises(UnscreeableError) as exc:
        parse_num(None, "buy_tax")
    assert exc.value.fields[0].name == "buy_tax"
    assert exc.value.fields[0].reason == "missing/null"


def test_case_2_empty_string_tax_field():
    """McKenzie's case 2 — the exact string that crashed tick 1964."""
    with pytest.raises(UnscreeableError) as exc:
        parse_num("", "sell_tax")
    f = exc.value.fields[0]
    assert f.name == "sell_tax"
    assert f.raw == ""
    assert "empty/unknown" in f.reason


def test_case_3_malformed_numeric():
    """McKenzie's case 3: 'abc' and '12.34.56' are malformed."""
    for bad in ("abc", "12.34.56", "1,5", "0x10", "--5"):
        with pytest.raises(UnscreeableError) as exc:
            parse_num(bad, "buy_tax")
        assert exc.value.fields[0].reason == "malformed numeric"


def test_case_4_whitespace_only():
    """Whitespace-only is empty, not a number. A subtle fail-open."""
    for blank in ("   ", "\t", "\n", " \t\n "):
        with pytest.raises(UnscreeableError):
            parse_num(blank, "buy_tax")


def test_case_5_missing_key_is_unscreeable():
    """An absent key must not read as 0. Source uses .get(k) -> None."""
    source = {"sell_tax": "1.0"}   # buy_tax key simply absent
    with pytest.raises(UnscreeableError) as exc:
        parse_fields(source, GOPLUS_NUMERIC_FIELDS, check="goplus")
    names = [f.name for f in exc.value.fields]
    assert "buy_tax" in names


def test_case_6_goplus_question_mark_token():
    """GoPlus returns "?" for GITLAWB. That is UNKNOWN, not zero."""
    with pytest.raises(UnscreeableError) as exc:
        parse_num("?", "buy_tax")
    assert "empty/unknown" in exc.value.fields[0].reason


def test_nan_and_inf_rejected():
    """NaN compares false against every threshold and would pass a gate."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(UnscreeableError) as exc:
            parse_num(bad, "buy_tax")
        assert exc.value.fields[0].reason == "nan/inf"


def test_bool_rejected_where_number_expected():
    """bool subclasses int; a flag is not a tax rate of 0 or 1."""
    with pytest.raises(UnscreeableError) as exc:
        parse_num(True, "buy_tax")
    assert "bool" in exc.value.fields[0].reason


def test_all_failures_collected_not_just_the_first():
    """One error naming three bad fields beats three errors."""
    source = {"buy_tax": "", "sell_tax": "abc",
              "top10_holder_concentration": None}
    with pytest.raises(UnscreeableError) as exc:
        parse_fields(source, GOPLUS_NUMERIC_FIELDS)
    assert len(exc.value.fields) == 3


def test_valid_values_still_parse():
    assert parse_num("0", "buy_tax") == 0.0
    assert parse_num(5, "buy_tax") == 5.0
    assert parse_num(" 7.5 ", "buy_tax") == 7.5


# ====================================================================== #
# 7. The gate: an unparseable field yields UNSCREENABLE, blocked
# ====================================================================== #

def _grader_with_payload(payload, journal=None):
    """A TokenGrader whose GoPlus call returns `payload`."""
    g = TG.TokenGrader(journal_path=journal)
    g._check_security = lambda addr: TG.TokenGrader._check_security.__wrapped__(g, addr) if False else _parse_like_real(g, payload)
    return g


def _parse_like_real(g, payload):
    """Replicate _check_security's parse step against a fixed payload."""
    import urllib.request
    if payload is None or not payload:
        raise UnscreeableError(
            [UnscreeableField("goplus_result", None,
                              "empty security payload")], check="goplus")
    result = payload.get("result", {}).get("0xtest", {})
    if not result:
        raise UnscreeableError(
            [UnscreeableField("goplus_result", None,
                              "empty security payload")], check="goplus")
    nums = parse_fields(result, GOPLUS_NUMERIC_FIELDS, check="goplus")
    return {
        "is_honeypot": result.get("is_honeypot") == "1",
        "cannot_sell": result.get("cannot_sell") == "1",
        "is_blacklist": result.get("is_blacklist") == "1",
        "hidden_owner": result.get("hidden_owner") == "1",
        "is_mintable": result.get("is_mintable") == "1",
        "is_pausable": result.get("is_pausable") == "1",
        "selfdestruct": result.get("selfdestruct") == "1",
        "buy_tax": nums["buy_tax"],
        "sell_tax": nums["sell_tax"],
        "is_verified": result.get("is_open_source") == "1",
        "no_lock": result.get("trust_list") != "1",
        "top10_holder_concentration": nums["top10_holder_concentration"],
    }


GOOD = {"is_honeypot": "0", "cannot_sell": "0", "is_blacklist": "0",
        "hidden_owner": "0", "is_mintable": "0", "is_pausable": "0",
        "selfdestruct": "0", "buy_tax": "0", "sell_tax": "0",
        "is_open_source": "1", "trust_list": "1",
        "top10_holder_concentration": "10"}


def test_gate_returns_unscreenable_on_question_mark(tmp_path):
    """The GITLABW case end-to-end through grade()."""
    jp = str(tmp_path / "u.jsonl")
    g = TG.TokenGrader(journal_path=jp)
    bad = dict(GOOD, buy_tax="?")
    g._check_security = lambda addr: _parse_like_real(g, {"result": {"0xtest": bad}})

    out = g.grade("0xTEST")
    assert out["verdict"] == TG.GRADE_UNSCREENABLE
    assert TG.GATE_UNSCREENABLE in out["reasons"]
    assert out["score"] == 0


def test_gate_rejects_empty_string_tax(tmp_path):
    jp = str(tmp_path / "u.jsonl")
    g = TG.TokenGrader(journal_path=jp)
    bad = dict(GOOD, sell_tax="")
    g._check_security = lambda addr: _parse_like_real(g, {"result": {"0xtest": bad}})
    out = g.grade("0xTEST")
    assert out["verdict"] == TG.GRADE_UNSCREENABLE


def test_gate_rejects_null_tax(tmp_path):
    jp = str(tmp_path / "u.jsonl")
    g = TG.TokenGrader(journal_path=jp)
    bad = dict(GOOD, buy_tax=None)
    g._check_security = lambda addr: _parse_like_real(g, {"result": {"0xtest": bad}})
    assert g.grade("0xTEST")["verdict"] == TG.GRADE_UNSCREENABLE


def test_gate_rejects_malformed_tax(tmp_path):
    jp = str(tmp_path / "u.jsonl")
    g = TG.TokenGrader(journal_path=jp)
    bad = dict(GOOD, buy_tax="abc")
    g._check_security = lambda addr: _parse_like_real(g, {"result": {"0xtest": bad}})
    assert g.grade("0xTEST")["verdict"] == TG.GRADE_UNSCREENABLE


def test_clean_token_still_grades(tmp_path):
    """Regression: the fix must not block legitimate tokens."""
    jp = str(tmp_path / "u.jsonl")
    g = TG.TokenGrader(journal_path=jp)
    g._check_security = lambda addr: _parse_like_real(g, {"result": {"0xtest": GOOD}})
    out = g.grade("0xTEST")
    assert out["verdict"] != TG.GRADE_UNSCREENABLE
    assert TG.GATE_UNSCREENABLE not in out["reasons"]


# ====================================================================== #
# 8. Synthetic crash: ANY exception fails closed (WO #1 scope 3)
# ====================================================================== #

def test_synthetic_crash_inside_check_fails_closed(tmp_path):
    """A crash must NEVER emit a passing verdict."""
    jp = str(tmp_path / "u.jsonl")
    g = TG.TokenGrader(journal_path=jp)

    def boom(addr):
        raise RuntimeError("simulated upstream failure")

    g._check_security = boom
    out = g.grade("0xTEST")
    assert out["verdict"] == TG.GRADE_UNSCREENABLE, (
        f"crash produced a non-blocking verdict: {out['verdict']}")
    assert out["score"] == 0
    assert TG.GATE_UNSCREENABLE in out["reasons"]


def test_synthetic_crash_records_the_exception(tmp_path):
    jp = str(tmp_path / "u.jsonl")
    g = TG.TokenGrader(journal_path=jp)
    g._check_security = lambda a: (_ for _ in ()).throw(ZeroDivisionError("boom"))
    out = g.grade("0xTEST")
    assert out["unscreeable"]["verdict"] == "UNSCREENABLE"
    assert "boom" in json.dumps(out["unscreeable"])


def test_pre_fix_behaviour_would_have_been_fail_open():
    """Documents the exact regression being prevented.

    The old handler docked 20 points from 100 and continued, so a token
    scored 80 — a passing grade from a crashed check. This asserts the
    new handler does not reproduce that.
    """
    jp = tempfile.mktemp(suffix=".jsonl")
    g = TG.TokenGrader(journal_path=jp)
    g._check_security = lambda a: (_ for _ in ()).throw(ValueError("crash"))
    out = g.grade("0xTEST")
    assert out["score"] == 0, "crash must not leave a passing score"
    assert out["verdict"] == TG.GRADE_UNSCREENABLE


# ====================================================================== #
# 9. Journal: self-describing (scope 5)
# ====================================================================== #

def test_journal_line_names_field_and_raw_value(tmp_path):
    jp = str(tmp_path / "u.jsonl")
    g = TG.TokenGrader(journal_path=jp)
    bad = dict(GOOD, buy_tax="?")
    g._check_security = lambda addr: _parse_like_real(g, {"result": {"0xtest": bad}})
    g.grade("0xTEST")

    lines = [json.loads(l) for l in open(jp)]
    assert len(lines) == 1
    line = lines[0]
    assert line["verdict"] == "UNSCREENABLE"
    assert line["token"] == "0xtest"
    assert line["blocked"] is True
    field = line["fields"][0]
    assert field["field"] == "buy_tax"
    assert field["raw_value"] == "?"          # the raw value is recorded
    assert field["reason"]


def test_journal_records_every_offending_field(tmp_path):
    jp = str(tmp_path / "u.jsonl")
    g = TG.TokenGrader(journal_path=jp)
    bad = dict(GOOD, buy_tax="", sell_tax="abc",
               top10_holder_concentration=None)
    g._check_security = lambda addr: _parse_like_real(g, {"result": {"0xtest": bad}})
    g.grade("0xTEST")
    line = json.loads(open(jp).read().splitlines()[0])
    assert len(line["fields"]) == 3


def test_journal_failure_does_not_change_the_verdict(tmp_path):
    """Losing the audit line is bad; changing the verdict is worse."""
    g = TG.TokenGrader(journal_path="/proc/nonexistent-dir/x/y.jsonl")
    bad = dict(GOOD, buy_tax="?")
    g._check_security = lambda addr: _parse_like_real(g, {"result": {"0xtest": bad}})
    out = g.grade("0xTEST")
    assert out["verdict"] == TG.GRADE_UNSCREENABLE


# ====================================================================== #
# 10. Taxonomy: UNSCREENABLE is canonical and blocking
# ====================================================================== #

def test_unscreenable_is_a_verdict_not_a_penalty():
    assert TG.GRADE_UNSCREENABLE == "UNSCREENABLE"
    assert TG.GATE_UNSCREENABLE == "UNSCREENABLE"
    # It must be distinct from the honeypot verdict: unknown is not
    # evidence of malice, and conflating them corrupts the statistics.
    assert TG.GRADE_UNSCREENABLE != TG.GATE_HONEYPOT


def test_unscreenable_result_is_cacheable_and_self_describing(tmp_path):
    jp = str(tmp_path / "u.jsonl")
    g = TG.TokenGrader(journal_path=jp)
    bad = dict(GOOD, buy_tax="?")
    g._check_security = lambda addr: _parse_like_real(g, {"result": {"0xtest": bad}})
    first = g.grade("0xTEST")
    second = g.grade("0xTEST")   # cache hit
    assert first["verdict"] == second["verdict"] == TG.GRADE_UNSCREENABLE
