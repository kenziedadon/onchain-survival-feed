#!/usr/bin/env python3
"""Safety watchdog for base-trend-bot.

Posture contract (caps.json):
  - kill_switch must be true
  - live_trading must be false
  - If either flips, the bot must not broadcast live orders.

Modes:
  --init        Capture baseline (files + caps.json content). Refuses if posture is armed.
  --check       Compare current state against baseline; log alerts.
  --enforce     Like --check, plus remediate posture if armed (unless .armed_by_human exists).
  --force       Bypass the disarmed-posture gate on --init (use with care).

Provenance: this implementation originated on the PC (deployed 2026-09-22);
reviewed and fixed in v0.3.0 (2026-09-22):
  - _alert appends to the log instead of read-modify-write (old code crashed
    when the log file did not exist, and was O(n^2) + non-atomic).
  - heartbeat() now also writes .watchdog_heartbeat (mtime file), because
    the alert forwarder and dashboard read that path, not watchdog_state.json.
    The missing write was the cause of the 2026-09-22 stale-heartbeat CRITICAL.
  - _diff_caps redacts values for secret-like keys instead of logging them.
  - _read_caps rejects non-dict JSON instead of crashing later on .get/[]=.
  - .env scan also flags MNEMONIC / SEED_PHRASE, not just RPC_URL / PRIVATE_KEY.

WO16 (2026-10-15): runner liveness heartbeat check.
  - probe_wiring.py touches probe_runner.heartbeat once per loop iteration.
  - The watchdog checks staleness (mtime > 5 min) while caps read ARMED.
  - One alert per stale episode: new staleness + recovery, not every tick.
  - Staleness while DISARMED is expected (runner stopped) — no alert.
  - The watchdog NEVER starts, restarts, or signals the runner — alerting
    only. A restart stays McKenzie-at-the-PC with the passphrase, by design.
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.3.0"

BOT_DIR = Path(os.path.expanduser("~/base-trend-bot"))
BASELINE_FILE = BOT_DIR / "watchdog_baseline.json"
ALERT_LOG = BOT_DIR / "logs" / "watchdog_alerts.log"
STATE_FILE = BOT_DIR / "watchdog_state.json"
HEARTBEAT_FILE = BOT_DIR / ".watchdog_heartbeat"
ARM_MARKER = BOT_DIR / ".armed_by_human"

# WO16 — runner liveness heartbeat (probe_wiring.py touches this once per
# loop iteration).  Staleness while DISARMED is expected (runner stopped);
# while ARMED, a stale heartbeat means the runner may be hung.
RUNNER_HEARTBEAT_FILE = BOT_DIR / "probe_runner.heartbeat"
RUNNER_HEARTBEAT_STALE_S = 5 * 60  # 5 minutes

# Key-name fragments whose values must never appear in an alert or diff.
REDACT_KEY_PARTS = ("PRIVATE_KEY", "MNEMONIC", "SEED_PHRASE", "SECRET")

# .env variable names treated as secret material (values never logged).
SECRET_ENV_VARS = ("RPC_URL", "PRIVATE_KEY", "MNEMONIC", "SEED_PHRASE")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _read_caps():
    caps_path = BOT_DIR / "caps.json"
    try:
        with open(caps_path) as f:
            caps = json.load(f)
    except (OSError, ValueError):
        return None
    return caps if isinstance(caps, dict) else None


def _redacted(key, value_repr):
    if any(part in str(key).upper() for part in REDACT_KEY_PARTS):
        return "<redacted>"
    return value_repr


def _diff_caps(baseline_caps, current_caps):
    """Return (posture_changed, detail_lines).

    posture_changed is True only if kill_switch or live_trading differ.
    detail_lines describe every key-level change for the alert log, with
    secret-like values redacted.
    """
    if baseline_caps is None or current_caps is None:
        return (True, ["caps.json unreadable in one of the snapshots"])

    detail = []
    posture_changed = False

    all_keys = set(baseline_caps.keys()) | set(current_caps.keys())
    for key in sorted(all_keys):
        old = baseline_caps.get(key)
        new = current_caps.get(key)
        if old != new:
            if key in ("kill_switch", "live_trading"):
                posture_changed = True
            old_repr = _redacted(key, json.dumps(old))
            new_repr = _redacted(key, json.dumps(new))
            detail.append(f"caps.json key {key!r}: {old_repr} -> {new_repr}")

    return (posture_changed, detail)


def _collect_state():
    caps = _read_caps()
    files = {}
    for name in ["caps.json", "watchdog.py", "go_live_loop.py.DISABLED"]:
        p = BOT_DIR / name
        if p.exists():
            files[name] = _sha256_file(p)
    auto_arm_present = (BOT_DIR / "go_live_loop.py").exists()
    secrets = []
    env_path = BOT_DIR / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                name = line.split("=", 1)[0]
                if name in SECRET_ENV_VARS and "=" in line:
                    secrets.append(name)
        except OSError:
            pass
    return {
        "caps": caps,
        "files": files,
        "auto_arm_present": auto_arm_present,
        "secrets": secrets,
    }


def _load_baseline():
    if not BASELINE_FILE.exists():
        return None
    try:
        return json.loads(BASELINE_FILE.read_text())
    except (OSError, ValueError):
        return None


def _save_baseline(state):
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(json.dumps(state, indent=2))


def _alert(msg):
    line = f"[{_now()}] WATCHDOG ALERT: {msg}"
    print(line, flush=True)
    try:
        ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(ALERT_LOG, "a") as f:
            f.write(line + "\n")
    except OSError as exc:
        print(f"[{_now()}] WATCHDOG ERROR: could not write alert log: {exc}",
              flush=True)


def cmd_init(args):
    caps = _read_caps()
    if caps is None:
        print("WATCHDOG: caps.json unreadable -- cannot init", flush=True)
        return 2
    armed = caps.get("kill_switch") is not True or caps.get("live_trading") is True
    if armed and not args.force:
        print("WATCHDOG: posture is armed -- refusing --init. "
              "Use --force only if you intend to capture an armed baseline.", flush=True)
        return 2
    if armed and args.force:
        print("WATCHDOG: posture is armed -- capturing armed baseline with --force.", flush=True)
    state = {
        "created": _now(),
        "armed_at_init": armed,
        "files": _collect_state()["files"],
        "caps_content": caps,
    }
    _save_baseline(state)
    print("WATCHDOG: baseline captured (caps.json content + file hashes).", flush=True)
    return 0


def cmd_check(args):
    baseline = _load_baseline()
    if baseline is None:
        print("WATCHDOG: no baseline -- run --init first.", flush=True)
        return 2
    state = _collect_state()
    alerts = 0

    caps = state["caps"]
    base_caps = baseline.get("caps_content")

    if caps == "unreadable" or caps is None:
        _alert("caps.json missing or unreadable")
        alerts += 1
    else:
        posture_changed, diff_detail = _diff_caps(base_caps, caps)
        if posture_changed:
            _alert("SAFETY POSTURE CHANGED: " + " | ".join(diff_detail))
            alerts += 1
        elif diff_detail:
            _alert("caps.json content changed since baseline (non-posture keys): " +
                   " | ".join(diff_detail))
            alerts += 1

    if state["auto_arm_present"]:
        _alert("go_live_loop.py EXISTS (auto-arm script reappeared) -- "
               "expected only go_live_loop.py.DISABLED")
        alerts += 1

    if state["secrets"]:
        _alert("SECRET MATERIAL DETECTED in .env: " +
               ", ".join(state["secrets"]) +
               " -- a signer may have been configured")
        alerts += 1

    base_files = baseline.get("files", {})
    for name, digest in state["files"].items():
        if name in base_files and base_files[name] != digest:
            _alert(f"file changed since baseline: {name}")
            alerts += 1
    if ("go_live_loop.py.DISABLED" in base_files
            and "go_live_loop.py.DISABLED" not in state["files"]):
        _alert("go_live_loop.py.DISABLED is gone")
        alerts += 1

    # WO16: runner liveness heartbeat check (one alert per stale episode)
    runner_alert = _check_runner_heartbeat()
    if runner_alert:
        _alert(runner_alert)
        alerts += 1

    heartbeat()
    if alerts:
        print(f"WATCHDOG: {alerts} alert(s) -- see {ALERT_LOG}", flush=True)
        return 1
    print("WATCHDOG: clean", flush=True)
    return 0


def heartbeat():
    """Record a heartbeat every reader can see.

    Writes the JSON state file AND the legacy .watchdog_heartbeat mtime
    file, because the alert forwarder and the dashboard read the latter.
    Never raises: a heartbeat failure must not break a check run.
    """
    now = _now()
    try:
        st = {}
        if STATE_FILE.exists():
            try:
                st = json.loads(STATE_FILE.read_text())
            except ValueError:
                st = {}
        st["last_check"] = now
        STATE_FILE.write_text(json.dumps(st, indent=2))
    except OSError:
        pass
    try:
        HEARTBEAT_FILE.write_text(now + "\n")
    except OSError:
        pass


# The arm-provenance contract (WO14): the SAME contract risk.py and
# policy.py already honor, pinned by the arm-gate key-shape suite. The
# watchdog's enforce path never consulted it and reverted a valid
# McKenzie arm on sight; WO14 teaches enforce the existing contract.
# Fail-closed stays the default — the exception is earned only by an
# EXACT match: arm_provenance == "arm_signal_v1" AND arm_authorized_by
# present AND arm_applied_at present and parseable. Every other shape
# (missing, partial, wrong value, malformed timestamp) force-disarms
# exactly as before.
ARM_PROVENANCE_VALUE = "arm_signal_v1"


def _arm_provenance_valid(caps):
    """True only on an EXACT contract match (WO14).

    - arm_provenance must equal "arm_signal_v1" exactly.
    - arm_authorized_by must be a non-empty string.
    - arm_applied_at must be a non-empty string that parses as an
      ISO-8601 timestamp (fromisoformat; a trailing 'Z' is accepted).

    Anything else — missing key, wrong value, empty string, partial
    shape, malformed timestamp — returns False, and the caller
    force-disarms exactly as it did before WO14. This is the safety
    layer: the exception is narrow on purpose.
    """
    if not isinstance(caps, dict):
        return False
    if caps.get("arm_provenance") != ARM_PROVENANCE_VALUE:
        return False
    authorized_by = caps.get("arm_authorized_by")
    if not isinstance(authorized_by, str) or not authorized_by.strip():
        return False
    applied_at = caps.get("arm_applied_at")
    if not isinstance(applied_at, str) or not applied_at.strip():
        return False
    try:
        datetime.fromisoformat(applied_at.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _check_runner_heartbeat():
    """WO16: check runner liveness heartbeat staleness.

    Returns an alert string or None.  Tracks state in watchdog_state.json
    so the watchdog alerts ONCE per stale episode — on new staleness and on
    recovery, not every tick.  Staleness while DISARMED is expected (the
    runner is stopped); while ARMED, a stale heartbeat means the runner
    may be hung.

    The watchdog NEVER starts, restarts, or signals the runner — alerting
    only.  A restart stays McKenzie-at-the-PC with the passphrase, by
    design.
    """
    hb_path = RUNNER_HEARTBEAT_FILE
    now = datetime.now(timezone.utc)

    # Check staleness
    if hb_path.exists():
        mtime = datetime.fromtimestamp(hb_path.stat().st_mtime, tz=timezone.utc)
        age_s = (now - mtime).total_seconds()
        is_stale = age_s > RUNNER_HEARTBEAT_STALE_S
        age_detail = f"age {age_s:.0f}s"
    else:
        is_stale = True  # Missing file = stale (runner not running)
        age_detail = "file missing"

    # Read caps posture — mirror the lane's arm contract (WO14):
    # kill_switch=False AND live_trading=True AND provenance valid.
    # A half-state (one flag flipped, lane unable to trade) is NOT armed;
    # staleness there is not a runner-failure signal.
    caps = _read_caps()
    armed = (caps is not None
             and caps.get("kill_switch") is False
             and caps.get("live_trading") is True
             and _arm_provenance_valid(caps))

    # Read state for tracking (one alert per stale episode)
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except ValueError:
            state = {}
    was_stale = state.get("runner_heartbeat_was_stale", False)

    # Determine alert
    if is_stale:
        if armed:
            if not was_stale:
                alert = (f"RUNNER HEARTBEAT STALE: probe_runner.heartbeat "
                         f"{age_detail} — runner may be hung")
            else:
                alert = None  # Already alerted for this episode
        else:
            alert = None  # DISARMED: staleness expected
    else:
        if was_stale:
            alert = ("RUNNER HEARTBEAT RECOVERED: probe_runner.heartbeat "
                     "is fresh again")
        else:
            alert = None

    # Update state.
    # F4: staleness observed while DISARMED is expected, not an episode.
    # Clear the flag on the disarmed-stale branch so an arm transition
    # into stale territory starts a fresh episode (fires exactly one alert).
    # Previously the flag was carried True from a disarmed-stale tick, causing
    # the first armed+stale tick to read "already alerted" and stay silent.
    # Side benefit: a runner starting while disarmed after a disarmed-stale
    # period produces no spurious RECOVERED alert (flag was cleared).
    if is_stale and not armed:
        state["runner_heartbeat_was_stale"] = False
    else:
        state["runner_heartbeat_was_stale"] = is_stale
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError:
        pass

    return alert


def cmd_enforce(args):
    rc = cmd_check(args)
    caps = _read_caps()
    if caps is None:
        return max(rc, 1)
    armed = caps.get("kill_switch") is not True or caps.get("live_trading") is True
    if not armed:
        return rc
    if ARM_MARKER.exists():
        _alert("caps.json shows armed state but .armed_by_human marker exists -- "
               "treating as human-authorized, not remediating")
        return max(rc, 1)
    # WO14: an EXACT valid-provenance arm is respected, not reverted.
    # Logged visibly every run so the audit trail shows the decision.
    if _arm_provenance_valid(caps):
        print("ARM RESPECTED: provenance valid "
              f"(arm_provenance={ARM_PROVENANCE_VALUE}, authorized_by present, "
              f"applied_at parseable) -- enforce will not revert the arm",
              flush=True)
        return max(rc, 1)
    caps["kill_switch"] = True
    caps["live_trading"] = False
    try:
        with open(BOT_DIR / "caps.json", "w") as f:
            json.dump(caps, f, indent=2)
        _alert("AUTO-REMEDIATED: kill_switch restored to true, "
               "live_trading restored to false")
    except OSError as exc:
        _alert(f"FAILED to remediate caps.json: {exc}")
        return 2
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Safety watchdog for base-trend-bot.")
    ap.add_argument("--init", action="store_true", help="capture baseline")
    ap.add_argument("--check", action="store_true", help="check against baseline")
    ap.add_argument("--enforce", action="store_true",
                    help="check + remediate armed posture")
    ap.add_argument("--force", action="store_true",
                    help="allow --init when posture is armed")
    args = ap.parse_args(argv)
    if args.init:
        return cmd_init(args)
    if args.enforce:
        return cmd_enforce(args)
    if args.check:
        return cmd_check(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
