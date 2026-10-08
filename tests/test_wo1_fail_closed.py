"""WO #1 — fail-closed on missing/empty/malformed tax fields, plus the
integration defects found in review (2026-10-07).

McKenzie, 2026-09-30 20:47: "Empty tax fields reject as UNSCREENABLE.
No exceptions, no defaults."

The incident: at orchestrator tick 1964, two discoveries (Surplus,
GITLAWB) crashed GoPlus parsing with `could not convert string to
float: ''`. The crash was caught by a handler that docked 20 points and
continued, so the check reported BOTH tokens "not honeypot, renounced".
A crashed check produced a PASSING verdict.

REVIEW FIXES (defect list, 2026-10-07):
  #1 wrong GoPlus key names (cannot_sell/is_blacklist/is_pausable)
  #2 top10_holder_concentration is not a GoPlus field
  #3 gate ordering: honeypot must outrank a tax-parse miss
  #4 fail-open WITH logging on source errors
  #5 tests must exercise the REAL _check_security with REAL payloads

The integration tests below feed genuine GoPlus responses (captured
2026-10-07, fixtures/goplus_live_20261007.json) through the real
_check_security via a stubbed urlopen. They do NOT monkeypatch
_check_security itself — the previous suite did, which is why it
measured the parser and never the integration.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from safe_parse import (GOPLUS_NUMERIC_FIELDS, UnscreenableError,
                        UnscreenableField, parse_fields, parse_num)
import token_grade as TG

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "goplus_live_20261007.json")


def _load_fixtures():
    with open(FIXTURES) as fh:
        return json.load(fh)


class _FakeResp:
    """Minimal urlopen context manager returning a canned JSON body."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _grader_with_live_payload(monkeypatch, payload, journal_path):
    """A TokenGrader whose urlopen returns `payload` for the GoPlus call.

    Critically: _check_security itself is NOT replaced. The real method
    parses the real payload shape — that is the point of defect #5.
    """
    g = TG.TokenGrader(journal_path=journal_path)
    g.miss_path = str(journal_path) + ".misses"
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, *a, **k: _FakeResp(payload))
    return g


# ====================================================================== #
# 1-6. Parsing: every unparseable shape raises, never defaults
# ====================================================================== #

def test_case_1_null_tax_field():
    """McKenzie's case 1: a null tax field is unscreenable, not 0."""
    with pytest.raises(UnscreenableError) as exc:
        parse_num(None, "buy_tax")
    assert exc.value.fields[0].name == "buy_tax"
    assert exc.value.fields[0].reason == "missing/null"


def test_case_2_empty_string_tax_field():
    """McKenzie's case 2 — the exact string that crashed tick 1964."""
    with pytest.raises(UnscreenableError) as exc:
        parse_num("", "sell_tax")
    f = exc.value.fields[0]
    assert f.name == "sell_tax"
    assert f.raw == ""
    assert "empty/unknown" in f.reason


def test_case_3_malformed_numeric():
    """McKenzie's case 3: 'abc' and '12.34.56' are malformed."""
    for bad in ("abc", "12.34.56", "1,5", "0x10", "--5"):
        with pytest.raises(UnscreenableError) as exc:
            parse_num(bad, "buy_tax")
        assert exc.value.fields[0].reason == "malformed numeric"


def test_case_4_whitespace_only():
    """Whitespace-only is empty, not a number. A subtle fail-open."""
    for blank in ("   ", "\t", "\n", " \t\n "):
        with pytest.raises(UnscreenableError):
            parse_num(blank, "buy_tax")


def test_case_5_missing_key_is_unscreenable():
    """An absent key must not read as 0. Source uses .get(k) -> None."""
    source = {"sell_tax": "1.0"}   # buy_tax key simply absent
    with pytest.raises(UnscreenableError) as exc:
        parse_fields(source, GOPLUS_NUMERIC_FIELDS, check="goplus")
    names = [f.name for f in exc.value.fields]
    assert "buy_tax" in names


def test_case_6_goplus_question_mark_token():
    """GoPlus returns "?" for GITLAWB. That is UNKNOWN, not zero."""
    with pytest.raises(UnscreenableError) as exc:
        parse_num("?", "buy_tax")
    assert "empty/unknown" in exc.value.fields[0].reason


def test_nan_and_inf_rejected():
    """NaN compares false against every threshold and would pass a gate."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(UnscreenableError) as exc:
            parse_num(bad, "buy_tax")
        assert exc.value.fields[0].reason == "nan/inf"


def test_bool_rejected_where_number_expected():
    """bool subclasses int; a flag is not a tax rate of 0 or 1."""
    with pytest.raises(UnscreenableError) as exc:
        parse_num(True, "buy_tax")
    assert "bool" in exc.value.fields[0].reason


def test_all_failures_collected_not_just_the_first():
    """One error naming every bad field beats one error per field.

    Defect #2: top10_holder_concentration was removed from
    GOPLUS_NUMERIC_FIELDS because GoPlus does not return it. The spec is
    now buy_tax + sell_tax only, so a two-bad-field source yields two.
    """
    source = {"buy_tax": "", "sell_tax": "abc"}
    with pytest.raises(UnscreenableError) as exc:
        parse_fields(source, GOPLUS_NUMERIC_FIELDS)
    assert len(exc.value.fields) == 2
    assert {f.name for f in exc.value.fields} == {"buy_tax", "sell_tax"}


def test_top10_concentration_is_not_a_goplus_field():
    """Defect #2 regression: the phantom field must stay out of the spec.

    GoPlus token_security/8453 was probed across real Base tokens
    (2026-10-07) and never returned top10_holder_concentration. Its
    presence in GOPLUS_NUMERIC_FIELDS made parse_fields raise for every
    real token — 100% UNSCREENABLE on the live path.
    """
    assert "top10_holder_concentration" not in GOPLUS_NUMERIC_FIELDS
    assert set(GOPLUS_NUMERIC_FIELDS) == {"buy_tax", "sell_tax"}


def test_valid_values_still_parse():
    """Sanity: real numbers still parse to floats."""
    assert parse_num("0", "buy_tax") == 0.0
    assert parse_num(5, "buy_tax") == 5.0
    assert parse_num(" 7.5 ", "buy_tax") == 7.5


# ====================================================================== #
# 7. Real-payload integration (defect #5)
#    These drive the REAL _check_security against REAL GoPlus bodies.
# ====================================================================== #

def test_real_payload_clean_token_is_screened(monkeypatch, tmp_path):
    """BOAR (real GoPlus body) must screen clean, not UNSCREENABLE.

    Before the fix this returned UNSCREENABLE because the phantom
    top10_holder_concentration key was absent.
    """
    fx = _load_fixtures()
    jp = str(tmp_path / "u.jsonl")
    g = _grader_with_live_payload(monkeypatch, fx["boar"]["response"], jp)
    sec = g._check_security(fx["boar"]["request_address"].lower())
    assert sec["parse_tax_ok"] is True
    assert sec["is_honeypot"] is False
    assert sec["is_blacklisted"] is False
    assert sec["is_verified"] is True


def test_real_payload_blacklisted_token_trips_gate(monkeypatch, tmp_path):
    """Defect #1 regression: is_blacklisted must actually be read.

    The old code read `is_blacklist`, which no live response contains,
    so this gate could never fire. PEPE's real body has is_blacklisted=1.
    """
    fx = _load_fixtures()
    jp = str(tmp_path / "u.jsonl")
    g = _grader_with_live_payload(monkeypatch, fx["pepe"]["response"], jp)
    out = g.grade(fx["pepe"]["request_address"])
    assert out["verdict"] == TG.GRADE_BLOCK
    assert TG.GATE_BLACKLIST in out["reasons"]


def test_real_payload_key_names_exist_in_live_response():
    """Defect #1: every key _extract_security reads must exist live.

    This is the mechanical check the original suite lacked — it asserts
    the code's field names against a real GoPlus record rather than
    against a hand-built dict.
    """
    fx = _load_fixtures()
    rec = fx["boar"]["response"]["result"][fx["boar"]["request_address"].lower()]
    for key in ("is_honeypot", "cannot_buy", "is_blacklisted", "hidden_owner",
                "is_mintable", "transfer_pausable", "selfdestruct",
                "is_open_source"):
        assert key in rec, f"{key} missing from live GoPlus payload"
    # The old names must NOT be present — that is why the gates were dead.
    for dead in ("cannot_sell", "is_blacklist", "is_pausable", "trust_list"):
        assert dead not in rec, f"{dead} unexpectedly present; re-check mapping"


def test_gate_ordering_honeypot_beats_tax_parse_miss(monkeypatch, tmp_path):
    """Defect #3: an affirmative bad signal outranks a tax-parse miss.

    A honeypot whose tax fields are unparseable must return BLOCK with
    the honeypot reason — not UNSCREENABLE, which would bury the
    affirmative evidence the journal exists to carry.
    """
    payload = {"code": 1, "message": "OK", "result": {
        "0xhoneypot": {
            "is_honeypot": "1", "cannot_buy": "0", "is_blacklisted": "0",
            "hidden_owner": "0", "is_mintable": "0", "transfer_pausable": "0",
            "selfdestruct": "0", "is_open_source": "1",
            "buy_tax": "?", "sell_tax": "?",   # genuine parse failure
        }}}
    jp = str(tmp_path / "u.jsonl")
    g = _grader_with_live_payload(monkeypatch, payload, jp)
    out = g.grade("0xhoneypot")
    assert out["verdict"] == TG.GRADE_BLOCK
    assert TG.GATE_HONEYPOT in out["reasons"]


def test_source_error_fails_closed_and_logs(monkeypatch, tmp_path):
    """Defect #4: an API error is UNSCREENABLE and leaves a miss record.

    Before the fix the handler returned {} silently, the token graded
    SAFE/65, and nothing was logged — indistinguishable from a clean
    screen.
    """
    g = TG.TokenGrader(journal_path=str(tmp_path / "u.jsonl"))
    g.miss_path = str(tmp_path / "misses.jsonl")
    import urllib.request

    def boom(req, *a, **k):
        raise OSError("simulated rate limit")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    out = g.grade("0xdeadbeef00000000000000000000000000000001")
    assert out["verdict"] == TG.GRADE_UNSCREENABLE
    assert os.path.exists(g.miss_path), "source error must be logged"
    line = json.loads(open(g.miss_path).read().splitlines()[0])
    assert line["source_error"] is True
    assert "api_error" in line["reason"]


def test_null_result_wrapper_fails_closed(monkeypatch, tmp_path):
    """GoPlus returns {"code":5000,"result":null}. Must not crash.

    This is the live crash found during review: result was None and the
    next .get() raised AttributeError. Fail-closed means UNSCREENABLE —
    never a crash, and never a pass.
    """
    payload = {"code": 5000, "message": "system error", "result": None}
    jp = str(tmp_path / "u.jsonl")
    g = _grader_with_live_payload(monkeypatch, payload, jp)
    out = g.grade("0xdeadbeef00000000000000000000000000000002")
    assert out["verdict"] == TG.GRADE_UNSCREENABLE


def test_empty_result_dict_fails_closed(monkeypatch, tmp_path):
    """An empty result object is unknown, not a pass."""
    payload = {"code": 1, "message": "OK", "result": {}}
    jp = str(tmp_path / "u.jsonl")
    g = _grader_with_live_payload(monkeypatch, payload, jp)
    out = g.grade("0xdeadbeef00000000000000000000000000000003")
    assert out["verdict"] == TG.GRADE_UNSCREENABLE


def test_unsupported_tax_does_not_block(monkeypatch, tmp_path):
    """Empty-string tax is 'unsupported', not a verdict-killer.

    GoPlus emits "" for tax on Base DEXs it does not evaluate. Treating
    that as a parse failure made every real token UNSCREENABLE.
    """
    payload = {"code": 1, "message": "OK", "result": {
        "0xclean": {
            "is_honeypot": "0", "cannot_buy": "0", "is_blacklisted": "0",
            "hidden_owner": "0", "is_mintable": "0", "transfer_pausable": "0",
            "selfdestruct": "0", "is_open_source": "1",
            "buy_tax": "", "sell_tax": "",
        }}}
    jp = str(tmp_path / "u.jsonl")
    g = _grader_with_live_payload(monkeypatch, payload, jp)
    sec = g._check_security("0xclean")
    # No field FAILED to parse — empty string is "unsupported", a
    # distinct state from a malformed value. Both flags must be exact.
    assert sec["parse_tax_ok"] is True
    assert sec["tax_unsupported"] is True
    assert sec["buy_tax"] is None and sec["sell_tax"] is None
    assert sec["is_honeypot"] is False      # not treated as bad


def test_question_mark_tax_still_fails_closed(monkeypatch, tmp_path):
    """'?' is a genuine parse failure and must still block (WO #1)."""
    payload = {"code": 1, "message": "OK", "result": {
        "0xqm": {
            "is_honeypot": "0", "cannot_buy": "0", "is_blacklisted": "0",
            "hidden_owner": "0", "is_mintable": "0", "transfer_pausable": "0",
            "selfdestruct": "0", "is_open_source": "1",
            "buy_tax": "?", "sell_tax": "0",
        }}}
    jp = str(tmp_path / "u.jsonl")
    g = _grader_with_live_payload(monkeypatch, payload, jp)
    out = g.grade("0xqm")
    assert out["verdict"] == TG.GRADE_UNSCREENABLE


# ====================================================================== #
# 8. Journal behaviour (unchanged intent, updated for the new spec)
# ====================================================================== #

def test_journal_line_names_field_and_raw_value(monkeypatch, tmp_path):
    jp = str(tmp_path / "u.jsonl")
    payload = {"code": 1, "message": "OK", "result": {
        "0xtest": {
            "is_honeypot": "0", "cannot_buy": "0", "is_blacklisted": "0",
            "hidden_owner": "0", "is_mintable": "0", "transfer_pausable": "0",
            "selfdestruct": "0", "is_open_source": "1",
            "buy_tax": "?", "sell_tax": "0",
        }}}
    g = _grader_with_live_payload(monkeypatch, payload, jp)
    g.grade("0xtest")
    line = json.loads(open(jp).read().splitlines()[0])
    assert line["verdict"] == "UNSCREENABLE"
    assert line["token"] == "0xtest"
    assert line["blocked"] is True
    field = line["fields"][0]
    assert field["field"] == "buy_tax"
    assert field["raw_value"] == "?"
    assert field["reason"]


def test_journal_records_every_offending_field(monkeypatch, tmp_path):
    jp = str(tmp_path / "u.jsonl")
    payload = {"code": 1, "message": "OK", "result": {
        "0xtest": {
            "is_honeypot": "0", "cannot_buy": "0", "is_blacklisted": "0",
            "hidden_owner": "0", "is_mintable": "0", "transfer_pausable": "0",
            "selfdestruct": "0", "is_open_source": "1",
            "buy_tax": "?", "sell_tax": "abc",
        }}}
    g = _grader_with_live_payload(monkeypatch, payload, jp)
    g.grade("0xtest")
    line = json.loads(open(jp).read().splitlines()[0])
    assert len(line["fields"]) == 2
    assert {f["field"] for f in line["fields"]} == {"buy_tax", "sell_tax"}


def test_journal_failure_does_not_change_the_verdict(tmp_path):
    """Losing the audit line is bad; changing the verdict is worse."""
    g = TG.TokenGrader(journal_path="/proc/nonexistent-dir/x/y.jsonl")
    g._journal_rejection("0xtest", {"ts": "x", "verdict": "UNSCREENABLE",
                                    "unscreenable": {"check": "goplus",
                                                     "fields": []}})
    # Reaching here without raising is the assertion.


# ====================================================================== #
# 9. Taxonomy: UNSCREENABLE is canonical and blocking
# ====================================================================== #

def test_unscreenable_is_a_verdict_not_a_penalty():
    assert TG.GRADE_UNSCREENABLE == "UNSCREENABLE"
    assert TG.GATE_UNSCREENABLE == "UNSCREENABLE"
    # It must be distinct from the honeypot verdict: unknown is not
    # evidence of malice, and conflating them corrupts the statistics.
    assert TG.GRADE_UNSCREENABLE != TG.GATE_HONEYPOT


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
    assert out["unscreenable"]["verdict"] == "UNSCREENABLE"
    assert "boom" in json.dumps(out["unscreenable"])


def test_pre_fix_behaviour_would_have_been_fail_open(tmp_path):
    """Documents the exact regression being prevented.

    The old handler docked 20 points from 100 and continued, so a token
    scored 80 — a passing grade from a crashed check. This asserts the
    new handler does not reproduce that.
    """
    jp = str(tmp_path / "pre.jsonl")
    g = TG.TokenGrader(journal_path=jp)
    g._check_security = lambda a: (_ for _ in ()).throw(ValueError("crash"))
    out = g.grade("0xTEST")
    assert out["score"] == 0, "crash must not leave a passing score"
    assert out["verdict"] == TG.GRADE_UNSCREENABLE


def test_unscreenable_result_is_cacheable_and_self_describing(tmp_path):
    jp = str(tmp_path / "u.jsonl")
    g = TG.TokenGrader(journal_path=jp)
    g._check_security = lambda a: (_ for _ in ()).throw(
        UnscreenableError([UnscreenableField("buy_tax", "?", "malformed")],
                          check="goplus"))
    first = g.grade("0xTEST")
    second = g.grade("0xTEST")   # cache hit
    assert first["verdict"] == second["verdict"] == TG.GRADE_UNSCREENABLE
    assert first["unscreenable"]["check"] == "goplus"
    assert first["score"] == 0
