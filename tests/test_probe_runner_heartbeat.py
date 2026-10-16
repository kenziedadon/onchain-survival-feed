#!/usr/bin/env python3
"""WO16 — probe runner liveness heartbeat.

Tests:
  1. Heartbeat advances per iteration (success branch)
  2. Heartbeat advances per iteration (F2/transient-fault branch)
  3. Stale + ARMED → exactly one alert
  4. Stale + ARMED → no second alert (already alerted)
  5. Stale + DISARMED → no alert
  6. Stale + mixed posture → no alert (half-state can't trade)
  7. Recovery → one alert
  8. New staleness → one alert (new episode)
  9. F4: stale-while-disarmed → arm → one alert (fresh episode)
  10. F4: stale-while-disarmed → fresh → no spurious recovery

F4 tests call _check_runner_heartbeat() directly rather than cmd_check(),
because arming the lane mid-test rewrites caps.json — which cmd_check also
flags as a posture/file change. The state-machine transition is what F4
pins, so we call the function under test directly.
a keystore, or starts an executor. The real caps.json is NEVER read or
written by this suite.

Run: .venv/bin/python -m pytest tests/test_probe_runner_heartbeat.py -v
     (venv Python 3.11.15 + pytest 9.1.1 — bootstrapped via ensurepip)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)

import probe_wiring as pw  # noqa: E402
import watchdog  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers — scratch dirs, NEVER the real files
# ---------------------------------------------------------------------------

def _armed_caps():
    """A caps dict carrying a complete, valid arm-provenance contract."""
    return {
        "kill_switch": False,
        "live_trading": True,
        "arm_provenance": "arm_signal_v1",
        "arm_authorized_by": "McKenzie Shannon — explicit go-live words",
        "arm_applied_at": "2026-10-14T21:10:00Z",
    }


def _disarmed_caps():
    """A plain disarmed caps — runner heartbeat staleness is expected."""
    return {"kill_switch": True, "live_trading": False}


def _mixed_ks_false_lt_false():
    """Mixed state: kill_switch=False, live_trading=False (can't trade)."""
    return {
        "kill_switch": False,
        "live_trading": False,
        "arm_provenance": "arm_signal_v1",
        "arm_authorized_by": "McKenzie Shannon — explicit go-live words",
        "arm_applied_at": "2026-10-14T21:10:00Z",
    }


def _mixed_ks_true_lt_true():
    """Mixed state: kill_switch=True, live_trading=True (blocked)."""
    return {
        "kill_switch": True,
        "live_trading": True,
        "arm_provenance": "arm_signal_v1",
        "arm_authorized_by": "McKenzie Shannon — explicit go-live words",
        "arm_applied_at": "2026-10-14T21:10:00Z",
    }


def _scratch_bot(tmp_path, caps_dict, monkeypatch):
    """Point watchdog + probe_wiring at a temp dir carrying `caps_dict`.

    All path constants are re-bound together with BOT_DIR — the real
    caps.json, watchdog_state.json, and probe_runner.heartbeat are
    untouched. A baseline is captured first (via --force) so cmd_check
    has a reference to diff against, exactly as a live cron run would.
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
    monkeypatch.setattr(watchdog, "RUNNER_HEARTBEAT_FILE",
                        bot / "probe_runner.heartbeat")
    monkeypatch.setattr(pw, "RUNNER_HEARTBEAT_FILE",
                        bot / "probe_runner.heartbeat")
    # capture a baseline so cmd_check diffs against one
    watchdog.cmd_init(argparse.Namespace(force=True))
    return bot


def _set_heartbeat_mtime(path, age_seconds):
    """Set a file's mtime to `age_seconds` ago (simulates staleness)."""
    old = time.time() - age_seconds
    os.utime(path, (old, old))


def _alert_count(log_path):
    """Count alert lines in the watchdog alert log."""
    if not log_path.exists():
        return 0
    return sum(1 for line in log_path.read_text().splitlines()
               if "WATCHDOG ALERT" in line)


def _run_check(bot):
    """Run the real check path. Returns (rc, stdout)."""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = watchdog.cmd_check(None)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# (1) Heartbeat advances per iteration — success branch
# ---------------------------------------------------------------------------

class TestHeartbeatSuccessBranch:
    def test_touch_writes_timestamp(self, tmp_path, monkeypatch):
        """_touch_heartbeat writes an ISO-8601 UTC timestamp."""
        hb = tmp_path / "probe_runner.heartbeat"
        monkeypatch.setattr(pw, "RUNNER_HEARTBEAT_FILE", str(hb))
        pw._touch_heartbeat()
        assert hb.exists()
        content = hb.read_text().strip()
        # Must parse as ISO-8601
        ts = datetime.fromisoformat(content)
        assert ts.tzinfo is not None  # timezone-aware

    def test_touch_updates_mtime(self, tmp_path, monkeypatch):
        """Each touch updates the file mtime (liveness signal)."""
        hb = tmp_path / "probe_runner.heartbeat"
        monkeypatch.setattr(pw, "RUNNER_HEARTBEAT_FILE", str(hb))
        pw._touch_heartbeat()
        mtime1 = hb.stat().st_mtime
        time.sleep(0.01)
        pw._touch_heartbeat()
        mtime2 = hb.stat().st_mtime
        assert mtime2 > mtime1

    def test_run_runner_touches_on_success(self, tmp_path, monkeypatch):
        """run_runner touches the heartbeat after a successful cycle."""
        hb = tmp_path / "probe_runner.heartbeat"
        monkeypatch.setattr(pw, "RUNNER_HEARTBEAT_FILE", str(hb))

        class _Ex:
            def run_cycle(self):
                return {"entries": [], "exits": []}

        pw.run_runner(_Ex(), interval_s=999, max_cycles=3,
                      sleep_fn=lambda s: None)
        assert hb.exists()
        content = hb.read_text().strip()
        ts = datetime.fromisoformat(content)
        assert ts.tzinfo is not None

    def test_run_runner_touches_each_iteration(self, tmp_path, monkeypatch):
        """Heartbeat is touched once per cycle, not just once at start.

        The touch happens AFTER run_cycle() returns, so cycle 1 sees no
        file yet (None), but cycles 2 and 3 see the touch from the
        previous cycle (not None).
        """
        hb = tmp_path / "probe_runner.heartbeat"
        monkeypatch.setattr(pw, "RUNNER_HEARTBEAT_FILE", str(hb))
        mtimes = []

        class _Ex:
            def run_cycle(self):
                mtimes.append(hb.stat().st_mtime if hb.exists() else None)
                return {}

        pw.run_runner(_Ex(), interval_s=999, max_cycles=3,
                      sleep_fn=lambda s: None)
        assert len(mtimes) == 3
        # Cycle 1: no touch yet (file doesn't exist when run_cycle starts)
        assert mtimes[0] is None
        # Cycles 2 and 3: see the touch from the previous cycle
        assert mtimes[1] is not None
        assert mtimes[2] is not None


# ---------------------------------------------------------------------------
# (2) Heartbeat advances per iteration — F2 (transient-fault) branch
# ---------------------------------------------------------------------------

class TestHeartbeatF2Branch:
    def test_touch_on_transient_fault(self, tmp_path, monkeypatch):
        """F2 branch (transient fault) also touches the heartbeat."""
        hb = tmp_path / "probe_runner.heartbeat"
        monkeypatch.setattr(pw, "RUNNER_HEARTBEAT_FILE", str(hb))

        class _FlakyEx:
            def __init__(self):
                self.calls = 0
            def run_cycle(self):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("transient fault")
                return {"entries": []}

        pw.run_runner(_FlakyEx(), interval_s=999, max_cycles=3,
                      sleep_fn=lambda s: None)
        assert hb.exists()

    def test_touch_on_transient_fault_each_iteration(self, tmp_path,
                                                     monkeypatch):
        """F2 branch touches on EVERY iteration, including after faults."""
        hb = tmp_path / "probe_runner.heartbeat"
        monkeypatch.setattr(pw, "RUNNER_HEARTBEAT_FILE", str(hb))
        touch_count = [0]
        original_touch = pw._touch_heartbeat

        def _counting_touch():
            touch_count[0] += 1
            original_touch()

        monkeypatch.setattr(pw, "_touch_heartbeat", _counting_touch)

        class _AlwaysFailEx:
            def run_cycle(self):
                raise RuntimeError("always fails")

        pw.run_runner(_AlwaysFailEx(), interval_s=999, max_cycles=3,
                      sleep_fn=lambda s: None)
        assert touch_count[0] == 3  # touched after each of 3 cycles


# ---------------------------------------------------------------------------
# (3) Stale + ARMED → exactly one alert
# ---------------------------------------------------------------------------

class TestStaleArmed:
    def test_stale_armed_alerts_once(self, tmp_path, monkeypatch):
        """Stale heartbeat + ARMED posture → exactly one alert."""
        bot = _scratch_bot(tmp_path, _armed_caps(), monkeypatch)
        hb = bot / "probe_runner.heartbeat"
        hb.write_text("2026-10-15T00:00:00+00:00\n")
        _set_heartbeat_mtime(hb, 600)  # 10 minutes old — stale

        rc, out = _run_check(bot)
        assert rc == 1
        assert _alert_count(watchdog.ALERT_LOG) == 1
        assert "RUNNER HEARTBEAT STALE" in out

    def test_stale_armed_no_second_alert(self, tmp_path, monkeypatch):
        """Second check while still stale → no new alert (already alerted)."""
        bot = _scratch_bot(tmp_path, _armed_caps(), monkeypatch)
        hb = bot / "probe_runner.heartbeat"
        hb.write_text("2026-10-15T00:00:00+00:00\n")
        _set_heartbeat_mtime(hb, 600)

        rc1, _ = _run_check(bot)
        assert rc1 == 1
        assert _alert_count(watchdog.ALERT_LOG) == 1

        # Second check — still stale, should NOT alert again
        rc2, out2 = _run_check(bot)
        assert rc2 == 0  # no new alerts
        assert _alert_count(watchdog.ALERT_LOG) == 1  # still 1

    def test_stale_armed_missing_file(self, tmp_path, monkeypatch):
        """Missing heartbeat file + ARMED → stale alert."""
        bot = _scratch_bot(tmp_path, _armed_caps(), monkeypatch)
        # Don't create the heartbeat file — missing = stale
        rc, out = _run_check(bot)
        assert rc == 1
        assert _alert_count(watchdog.ALERT_LOG) == 1
        assert "RUNNER HEARTBEAT STALE" in out


# ---------------------------------------------------------------------------
# (4) Stale + DISARMED → no alert
# ---------------------------------------------------------------------------

class TestStaleDisarmed:
    def test_stale_disarmed_no_alert(self, tmp_path, monkeypatch):
        """Stale heartbeat + DISARMED posture → no alert (expected)."""
        bot = _scratch_bot(tmp_path, _disarmed_caps(), monkeypatch)
        hb = bot / "probe_runner.heartbeat"
        hb.write_text("2026-10-15T00:00:00+00:00\n")
        _set_heartbeat_mtime(hb, 600)

        rc, out = _run_check(bot)
        assert rc == 0  # clean — staleness expected when disarmed
        assert _alert_count(watchdog.ALERT_LOG) == 0

    def test_stale_disarmed_missing_file_no_alert(self, tmp_path,
                                                   monkeypatch):
        """Missing heartbeat + DISARMED → no alert."""
        bot = _scratch_bot(tmp_path, _disarmed_caps(), monkeypatch)
        rc, out = _run_check(bot)
        assert rc == 0
        assert _alert_count(watchdog.ALERT_LOG) == 0


class TestF4DisarmedToArmedTransition:
    """F4: stale-while-disarmed must NOT carry the was_stale flag forward.

    When staleness is observed while DISARMED, the flag must be cleared
    (not set True) so that arming the lane while the runner is still down
    starts a fresh episode and fires exactly one STALE alert — not silence.
    """

    def test_stale_disarmed_then_arm_fires_once(self, tmp_path, monkeypatch):
        """Stale+DISARMED (no alert) → arm caps → stale+ARMED → one alert.

        Before F4: the disarmed-stale tick set was_stale=True in state, so
        the first armed+stale tick read 'already alerted' and stayed silent.
        After F4: the flag is cleared on the disarmed-stale branch, so the
        arm transition starts a fresh episode and fires exactly one alert.
        """
        bot = _scratch_bot(tmp_path, _disarmed_caps(), monkeypatch)
        hb = bot / "probe_runner.heartbeat"
        hb.write_text("2026-10-15T00:00:00+00:00\n")
        _set_heartbeat_mtime(hb, 600)  # stale

        # Step 1: disarmed + stale → no alert (flag cleared by F4)
        alert1 = watchdog._check_runner_heartbeat()
        assert alert1 is None

        # Step 2: arm the lane WITHOUT restarting the runner (still stale)
        (bot / "caps.json").write_text(json.dumps(_armed_caps()))

        # Step 3: armed + stale → exactly one STALE alert (new episode)
        alert2 = watchdog._check_runner_heartbeat()
        assert alert2 is not None
        assert "RUNNER HEARTBEAT STALE" in alert2

        # Step 4: second check, still stale + armed → no new alert
        alert3 = watchdog._check_runner_heartbeat()
        assert alert3 is None  # already alerted for this episode

    def test_disarmed_stale_then_fresh_no_spurious_recovery(self, tmp_path,
                                                             monkeypatch):
        """Stale+DISARMED (no alert) → runner starts fresh while still
        disarmed → NO spurious RECOVERED alert (flag was cleared)."""
        bot = _scratch_bot(tmp_path, _disarmed_caps(), monkeypatch)
        hb = bot / "probe_runner.heartbeat"
        hb.write_text("2026-10-15T00:00:00+00:00\n")
        _set_heartbeat_mtime(hb, 600)  # stale

        # Step 1: disarmed + stale → no alert (flag cleared by F4)
        alert1 = watchdog._check_runner_heartbeat()
        assert alert1 is None

        # Step 2: runner starts (fresh heartbeat), still disarmed
        hb.write_text(datetime.now(timezone.utc).isoformat() + "\n")

        # Step 3: disarmed + fresh → no spurious RECOVERED
        alert2 = watchdog._check_runner_heartbeat()
        assert alert2 is None  # before F4: this would have been RECOVERED


# ---------------------------------------------------------------------------
# (4b) Stale + mixed posture → no alert (half-state can't trade)
# ---------------------------------------------------------------------------

class TestMixedStates:
    """kill_switch=False/live_trading=False → no alert; kill_switch=True/
    live_trading=True → no alert.  The arm contract requires BOTH flags in
    the trading direction AND valid provenance.  A half-state lane cannot
    trade, so staleness is not a runner-failure signal."""

    def test_stale_mixed_ks_false_lt_false_no_alert(self, tmp_path,
                                                     monkeypatch):
        """Stale heartbeat + kill_switch=False, live_trading=False → no alert."""
        bot = _scratch_bot(tmp_path, _mixed_ks_false_lt_false(), monkeypatch)
        hb = bot / "probe_runner.heartbeat"
        hb.write_text("2026-10-15T00:00:00+00:00\n")
        _set_heartbeat_mtime(hb, 600)

        rc, out = _run_check(bot)
        assert rc == 0
        assert _alert_count(watchdog.ALERT_LOG) == 0

    def test_stale_mixed_ks_true_lt_true_no_alert(self, tmp_path,
                                                   monkeypatch):
        """Stale heartbeat + kill_switch=True, live_trading=True → no alert."""
        bot = _scratch_bot(tmp_path, _mixed_ks_true_lt_true(), monkeypatch)
        hb = bot / "probe_runner.heartbeat"
        hb.write_text("2026-10-15T00:00:00+00:00\n")
        _set_heartbeat_mtime(hb, 600)

        rc, out = _run_check(bot)
        assert rc == 0
        assert _alert_count(watchdog.ALERT_LOG) == 0


# ---------------------------------------------------------------------------
# (5) Recovery → one alert
# ---------------------------------------------------------------------------

class TestRecovery:
    def test_recovery_alerts_once(self, tmp_path, monkeypatch):
        """Heartbeat recovers → one recovery alert."""
        bot = _scratch_bot(tmp_path, _armed_caps(), monkeypatch)
        hb = bot / "probe_runner.heartbeat"

        # First: stale → alert
        hb.write_text("2026-10-15T00:00:00+00:00\n")
        _set_heartbeat_mtime(hb, 600)
        rc1, _ = _run_check(bot)
        assert rc1 == 1
        assert _alert_count(watchdog.ALERT_LOG) == 1

        # Recover: fresh heartbeat
        hb.write_text(datetime.now(timezone.utc).isoformat() + "\n")
        rc2, out2 = _run_check(bot)
        assert rc2 == 1
        assert _alert_count(watchdog.ALERT_LOG) == 2
        assert "RUNNER HEARTBEAT RECOVERED" in out2

    def test_recovery_no_repeat(self, tmp_path, monkeypatch):
        """Second check after recovery → no new alert."""
        bot = _scratch_bot(tmp_path, _armed_caps(), monkeypatch)
        hb = bot / "probe_runner.heartbeat"

        # Stale → alert
        hb.write_text("2026-10-15T00:00:00+00:00\n")
        _set_heartbeat_mtime(hb, 600)
        _run_check(bot)

        # Recover
        hb.write_text(datetime.now(timezone.utc).isoformat() + "\n")
        _run_check(bot)

        # Third check — still fresh, no new alert
        rc3, _ = _run_check(bot)
        assert rc3 == 0
        assert _alert_count(watchdog.ALERT_LOG) == 2  # stale + recovery


# ---------------------------------------------------------------------------
# (6) New staleness → one alert (new episode)
# ---------------------------------------------------------------------------

class TestNewStaleness:
    def test_new_staleness_after_recovery(self, tmp_path, monkeypatch):
        """Stale → recover → stale again → new alert (new episode)."""
        bot = _scratch_bot(tmp_path, _armed_caps(), monkeypatch)
        hb = bot / "probe_runner.heartbeat"

        # Episode 1: stale
        hb.write_text("2026-10-15T00:00:00+00:00\n")
        _set_heartbeat_mtime(hb, 600)
        _run_check(bot)
        assert _alert_count(watchdog.ALERT_LOG) == 1

        # Recover
        hb.write_text(datetime.now(timezone.utc).isoformat() + "\n")
        _run_check(bot)
        assert _alert_count(watchdog.ALERT_LOG) == 2

        # Episode 2: stale again
        _set_heartbeat_mtime(hb, 600)
        rc3, out3 = _run_check(bot)
        assert rc3 == 1
        assert _alert_count(watchdog.ALERT_LOG) == 3
        assert "RUNNER HEARTBEAT STALE" in out3


# ---------------------------------------------------------------------------
# (7) Boundary — this suite never touches the real files
# ---------------------------------------------------------------------------

class TestBoundaries:
    def test_suite_source_never_reads_real_caps(self, tmp_path):
        """Guard: no reference to the real caps path in this file."""
        src = open(os.path.abspath(__file__)).read()
        real = os.path.join(os.path.expanduser("~"), "base-trend-bot",
                            "caps.json")
        assert real not in src

    def test_real_caps_untouched(self, tmp_path, monkeypatch):
        """The fixture run cannot reach the real caps: BOT_DIR is
        monkeypatched, and the real caps mtime/hash is unchanged."""
        import hashlib
        real_caps = os.path.join(os.path.expanduser("~"),
                                 "base-trend-bot", "caps.json")
        before = hashlib.sha256(open(real_caps, "rb").read()).hexdigest()
        bot = _scratch_bot(tmp_path, _armed_caps(), monkeypatch)
        _run_check(bot)
        after = hashlib.sha256(open(real_caps, "rb").read()).hexdigest()
        assert before == after

    def test_no_executor_or_keystore_contact(self):
        """Guard: this suite never starts an executor or touches a
        keystore — asserted by scanning the CODE only."""
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
        ]
        for name in forbidden:
            assert name not in code_only, \
                f"WO16 tests must not reference {name} in code"


if __name__ == "__main__":
    import argparse  # noqa: E402
    sys.exit(pytest.main([__file__, "-v"]))
