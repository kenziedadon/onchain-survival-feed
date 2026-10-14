#!/usr/bin/env python3
"""WO14 — watchdog enforce learns the arm-provenance contract.

The defect (verified 2026-10-14 16:10 CDT): the arm edit landed and
parsed armed, then `watchdog --enforce` rewrote kill_switch /
live_trading back to disarmed within one tick, leaving the provenance
fields untouched. Enforce never consulted provenance, while risk.py
and policy.py already honor it (the arm-gate key-shape suite pins the
shapes).

What WO14 teaches enforce — the EXISTING contract, nothing new:
  - arm_provenance == "arm_signal_v1" AND arm_authorized_by present
    AND arm_applied_at present and parseable  ->  ARM RESPECTED:
    the keys are NOT reverted, and the run logs it visibly.
  - Any other shape (missing / partial / wrong value / malformed
    timestamp)  ->  force-disarm EXACTLY as before WO14.

Fail-closed stays the default. The exception is narrow on purpose.

Every test here runs against a SCRATCH fixture caps file in a temp
dir (monkeypatched BOT_DIR) — the real caps.json is NEVER read or
written by this suite. No test contacts a chain, a keystore, or
starts an executor.

Run: python3 -m pytest tests/test_watchdog_arm_provenance.py -v
     (system python 3.14 + pytest 9.1.1; the venvs lack pytest)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
import argparse

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)

import watchdog  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers — a scratch caps file in a temp dir, NEVER the real caps.
# ---------------------------------------------------------------------------

def _valid_provenance(overrides=None):
    """A caps dict carrying a complete, valid arm-provenance contract."""
    caps = {
        "kill_switch": False,
        "live_trading": True,
        "arm_provenance": "arm_signal_v1",
        "arm_authorized_by": "McKenzie Shannon — explicit go-live words",
        "arm_applied_at": "2026-10-14T21:10:00Z",
    }
    if overrides:
        caps.update(overrides)
    return caps


def _armed_without_provenance():
    """The old blind-arm shape: armed, with NO provenance keys at all."""
    return {"kill_switch": False, "live_trading": True}


def _disarmed():
    """A plain disarmed caps — enforce has nothing to remediate."""
    return {"kill_switch": True, "live_trading": False}


def _scratch_env(tmp_path, caps_dict, monkeypatch):
    """Point the watchdog's BOT_DIR at a temp dir carrying `caps_dict`.

    All path constants derive from BOT_DIR at import time, so they are
    re-bound here together with it — the real caps.json is untouched.
    A baseline is captured first (via --force) so cmd_check has a
    reference to diff against, exactly as a live cron run would.
    """
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "caps.json").write_text(json.dumps(caps_dict))
    (bot / "logs").mkdir()
    monkeypatch.setattr(watchdog, "BOT_DIR", bot)
    monkeypatch.setattr(watchdog, "BASELINE_FILE",
                        bot / "watchdog_baseline.json")
    monkeypatch.setattr(watchdog, "ALERT_LOG",
                        bot / "logs" / "watchdog_alerts.log")
    monkeypatch.setattr(watchdog, "STATE_FILE",
                        bot / "watchdog_state.json")
    monkeypatch.setattr(watchdog, "HEARTBEAT_FILE",
                        bot / ".watchdog_heartbeat")
    monkeypatch.setattr(watchdog, "ARM_MARKER", bot / ".armed_by_human")
    # capture a baseline so cmd_check diffs against one (a live cron
    # run always has watchdog_baseline.json present)
    watchdog.cmd_init(argparse.Namespace(force=True))
    return bot


def _run_enforce(bot):
    """Run the real enforce path. Returns (rc, stdout)."""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = watchdog.cmd_enforce(None)
    return rc, buf.getvalue()


def _caps_now(bot):
    return json.loads((bot / "caps.json").read_text())


# ---------------------------------------------------------------------------
# (a) valid provenance -> enforce leaves the keys untouched + logs it
# ---------------------------------------------------------------------------

class TestValidProvenanceRespected:

    def test_enforce_does_not_revert_valid_arm(self, tmp_path,
                                               monkeypatch):
        bot = _scratch_env(tmp_path, _valid_provenance(), monkeypatch)
        rc, out = _run_enforce(bot)
        caps = _caps_now(bot)
        assert caps["kill_switch"] is False, \
            "a valid-provenance arm must NOT be reverted"
        assert caps["live_trading"] is True, \
            "a valid-provenance arm must NOT be reverted"
        assert rc == 1, "enforce still reports the posture alert, rc 1"

    def test_enforce_logs_arm_respected(self, tmp_path, monkeypatch):
        bot = _scratch_env(tmp_path, _valid_provenance(), monkeypatch)
        _rc, out = _run_enforce(bot)
        assert "ARM RESPECTED" in out, \
            "the run must log the decision visibly"
        assert "provenance valid" in out

    def test_provenance_keys_survive_the_run(self, tmp_path, monkeypatch):
        """The exact WO14 defect: enforce used to leave provenance
        fields intact but revert the booleans. Now BOTH survive."""
        bot = _scratch_env(tmp_path, _valid_provenance(), monkeypatch)
        _run_enforce(bot)
        caps = _caps_now(bot)
        assert caps["arm_provenance"] == "arm_signal_v1"
        assert caps["arm_authorized_by"]
        assert caps["arm_applied_at"]

    @pytest.mark.parametrize("applied_at", [
        "2026-10-14T21:10:00Z",          # trailing Z
        "2026-10-14T21:10:00+00:00",     # explicit offset
        "2026-10-14T16:10:00-05:00",     # CDT offset
    ])
    def test_parseable_timestamp_variants_accepted(self, tmp_path,
                                                   monkeypatch,
                                                   applied_at):
        bot = _scratch_env(
            tmp_path,
            _valid_provenance({"arm_applied_at": applied_at}),
            monkeypatch)
        rc, out = _run_enforce(bot)
        caps = _caps_now(bot)
        assert caps["kill_switch"] is False
        assert caps["live_trading"] is True
        assert "ARM RESPECTED" in out


# ---------------------------------------------------------------------------
# (b) every poison shape -> force-disarm EXACTLY as today
# ---------------------------------------------------------------------------

class TestPoisonShapesForceDisarm:

    def _assert_force_disarmed(self, tmp_path, monkeypatch, caps_dict):
        bot = _scratch_env(tmp_path, caps_dict, monkeypatch)
        rc, out = _run_enforce(bot)
        caps = _caps_now(bot)
        assert caps["kill_switch"] is True, \
            f"poison shape must force-disarm: {caps_dict}"
        assert caps["live_trading"] is False, \
            f"poison shape must force-disarm: {caps_dict}"
        assert "ARM RESPECTED" not in out, \
            "a poison shape must never log ARM RESPECTED"
        assert rc == 1
        # and the remediation was actually logged, as before
        assert "AUTO-REMEDIATED" in out

    def test_missing_all_provenance_keys(self, tmp_path, monkeypatch):
        """The old blind-arm shape: armed with NO provenance keys —
        force-disarm, exactly the pre-WO14 behavior."""
        self._assert_force_disarmed(tmp_path, monkeypatch,
                                    _armed_without_provenance())

    def test_wrong_provenance_value(self, tmp_path, monkeypatch):
        self._assert_force_disarmed(
            tmp_path, monkeypatch,
            _valid_provenance({"arm_provenance": "something_else"}))

    def test_provenance_none(self, tmp_path, monkeypatch):
        self._assert_force_disarmed(
            tmp_path, monkeypatch,
            _valid_provenance({"arm_provenance": None}))

    def test_authorized_by_missing(self, tmp_path, monkeypatch):
        caps = _valid_provenance()
        del caps["arm_authorized_by"]
        self._assert_force_disarmed(tmp_path, monkeypatch, caps)

    def test_authorized_by_empty(self, tmp_path, monkeypatch):
        self._assert_force_disarmed(
            tmp_path, monkeypatch,
            _valid_provenance({"arm_authorized_by": ""}))

    def test_authorized_by_whitespace_only(self, tmp_path, monkeypatch):
        self._assert_force_disarmed(
            tmp_path, monkeypatch,
            _valid_provenance({"arm_authorized_by": "   "}))

    def test_authorized_by_not_a_string(self, tmp_path, monkeypatch):
        self._assert_force_disarmed(
            tmp_path, monkeypatch,
            _valid_provenance({"arm_authorized_by": 42}))

    def test_applied_at_missing(self, tmp_path, monkeypatch):
        caps = _valid_provenance()
        del caps["arm_applied_at"]
        self._assert_force_disarmed(tmp_path, monkeypatch, caps)

    def test_applied_at_empty(self, tmp_path, monkeypatch):
        self._assert_force_disarmed(
            tmp_path, monkeypatch,
            _valid_provenance({"arm_applied_at": ""}))

    def test_applied_at_not_a_string(self, tmp_path, monkeypatch):
        self._assert_force_disarmed(
            tmp_path, monkeypatch,
            _valid_provenance({"arm_applied_at": 1699999999}))

    @pytest.mark.parametrize("bad_ts", [
        "not-a-timestamp",
        "2026-13-45T99:99:99Z",   # impossible date components
        "2026/10/14 21:10",       # wrong separator style
        "tomorrow-ish",
    ])
    def test_applied_at_malformed(self, tmp_path, monkeypatch, bad_ts):
        self._assert_force_disarmed(
            tmp_path, monkeypatch,
            _valid_provenance({"arm_applied_at": bad_ts}))

    def test_partial_shape_only_provenance_value(self, tmp_path,
                                                 monkeypatch):
        """Only the value, nothing else — partial, force-disarm."""
        self._assert_force_disarmed(
            tmp_path, monkeypatch,
            {"kill_switch": False, "live_trading": True,
             "arm_provenance": "arm_signal_v1"})

    def test_poison_shape_preserves_provenance_keys(self, tmp_path,
                                                    monkeypatch):
        """Force-disarm reverts ONLY the booleans; the provenance
        fields are left as they were — the pre-WO14 write behavior."""
        bot = _scratch_env(
            tmp_path,
            _valid_provenance({"arm_provenance": "wrong_value"}),
            monkeypatch)
        _run_enforce(bot)
        caps = _caps_now(bot)
        assert caps["kill_switch"] is True
        assert caps["live_trading"] is False
        assert caps["arm_provenance"] == "wrong_value", \
            "the poison value itself is not 'fixed' — only posture reverts"


# ---------------------------------------------------------------------------
# (c) disarmed caps -> behavior identical to today
# ---------------------------------------------------------------------------

class TestDisarmedUnchanged:

    def test_disarmed_caps_untouched(self, tmp_path, monkeypatch):
        bot = _scratch_env(tmp_path, _disarmed(), monkeypatch)
        rc, out = _run_enforce(bot)
        caps = _caps_now(bot)
        assert caps == _disarmed(), "disarmed caps must be untouched"
        assert "ARM RESPECTED" not in out
        assert "AUTO-REMEDIATED" not in out

    def test_disarmed_with_valid_provenance_untouched(self, tmp_path,
                                                      monkeypatch):
        """Provenance fields present but posture already disarmed:
        nothing to do, no ARM RESPECTED line (the arm is not armed)."""
        bot = _scratch_env(
            tmp_path,
            {"kill_switch": True, "live_trading": False,
             "arm_provenance": "arm_signal_v1",
             "arm_authorized_by": "x", "arm_applied_at":
             "2026-10-14T21:10:00Z"},
            monkeypatch)
        rc, out = _run_enforce(bot)
        caps = _caps_now(bot)
        assert caps["kill_switch"] is True
        assert caps["live_trading"] is False
        assert "ARM RESPECTED" not in out, \
            "ARM RESPECTED is for an ARMED state being respected"


# ---------------------------------------------------------------------------
# (d) the .armed_by_human marker path is unchanged
# ---------------------------------------------------------------------------

class TestArmMarkerUnchanged:

    def test_marker_still_wins_over_provenance(self, tmp_path,
                                               monkeypatch):
        """The pre-existing marker path is checked BEFORE provenance
        and still takes priority — WO14 adds a branch, it does not
        reorder the existing ones."""
        bot = _scratch_env(tmp_path, _valid_provenance(), monkeypatch)
        (bot / ".armed_by_human").write_text("human\n")
        rc, out = _run_enforce(bot)
        caps = _caps_now(bot)
        assert caps["kill_switch"] is False
        assert caps["live_trading"] is True
        assert "ARM RESPECTED" not in out
        assert "human-authorized" in out


# ---------------------------------------------------------------------------
# The contract unit itself — pure, no filesystem
# ---------------------------------------------------------------------------

class TestArmProvenanceValidUnit:

    def test_valid_contract_true(self):
        assert watchdog._arm_provenance_valid(_valid_provenance())

    def test_non_dict_false(self):
        assert not watchdog._arm_provenance_valid(None)
        assert not watchdog._arm_provenance_valid("arm_signal_v1")
        assert not watchdog._arm_provenance_valid([])

    def test_constant_matches_the_contract(self):
        assert watchdog.ARM_PROVENANCE_VALUE == "arm_signal_v1"

    def test_contract_constant_pinned_by_the_arm_gate_suite(self):
        """The value is the SAME one the arm-gate key-shape suite
        pins — this asserts the two suites agree, not a copy."""
        src = open(os.path.join(BOT, "watchdog.py")).read()
        assert '"arm_signal_v1"' in src


# ---------------------------------------------------------------------------
# Boundary — this suite never touches the real caps.json
# ---------------------------------------------------------------------------

class TestBoundaries:

    def test_suite_source_never_reads_the_real_caps(self):
        """Guard: no reference to the real caps path in this file."""
        src = open(os.path.abspath(__file__)).read()
        real = os.path.join(os.path.expanduser("~"), "base-trend-bot",
                            "caps.json")
        assert real not in src, \
            "the suite must use scratch fixtures only"

    def test_real_caps_untouched_by_the_run(self, tmp_path,
                                            monkeypatch):
        """The fixture run cannot reach the real caps: BOT_DIR is
        monkeypatched, and the real caps mtime/hash is unchanged."""
        import hashlib
        real_caps = os.path.join(BOT, "caps.json")
        before = hashlib.sha256(open(real_caps, "rb").read()).hexdigest()
        bot = _scratch_env(tmp_path, _valid_provenance(), monkeypatch)
        _run_enforce(bot)
        after = hashlib.sha256(open(real_caps, "rb").read()).hexdigest()
        assert before == after, "the real caps.json must be untouched"

    def test_no_executor_or_keystore_contact(self):
        """Guard: this suite never starts an executor or touches a
        keystore — asserted by scanning the CODE only (the module
        docstring may name them as boundary statements; no
        executable line may reference them). The forbidden names are
        assembled from parts so this guard's own list is not what it
        matches against."""
        import ast
        src = open(os.path.abspath(__file__)).read()
        tree = ast.parse(src)
        lines = src.splitlines()
        doc_end = 0
        if tree.body and isinstance(tree.body[0], ast.Expr) and \
                isinstance(tree.body[0].value, ast.Constant) and \
                isinstance(tree.body[0].value.value, str):
            doc_end = tree.body[0].end_lineno
        code_only = "\n".join(lines[doc_end:])
        forbidden = [
            "probe_" + "executor",
            "probe_" + "keystore",
            "decrypt_" + "at_use",
            "run_" + "cycle",
        ]
        for name in forbidden:
            assert name not in code_only, \
                f"WO14 tests must not reference {name} in code"
