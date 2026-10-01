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
  - heartbeat() now also writes .watchdog_heartbeat (mtime file), because the
    alert forwarder and dashboard read that path, not watchdog_state.json.
    The missing write was the cause of the 2026-09-22 stale-heartbeat CRITICAL.
  - _diff_caps redacts values for secret-like keys instead of logging them.
  - _read_caps rejects non-dict JSON instead of crashing later on .get/[]=.
  - .env scan also flags MNEMONIC / SEED_PHRASE, not just RPC_URL / PRIVATE_KEY.
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
