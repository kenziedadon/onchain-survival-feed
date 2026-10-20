#!/usr/bin/env python3
"""Discovery-only cleanup (idempotent, backed up).
Removes two stale/untradeable tokens from discovered.jsonl + scout_state.json
and records them in blocked_tokens.json to prevent re-discovery.

Does NOT touch: swap.js, caps.json, shadow_book.json, risk_state.json.
Re-checks kill_switch before running; aborts if not armed.
"""
import json, os, pathlib, datetime, shutil
from _atomic_write import atomic_jsonl_strings_write

HERE = pathlib.Path(__file__).resolve().parent
TS = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")

SURPLUS = "0xc52aedec3374422d7510e294cfaa90799595cba3"
NONSTD = "0xed664536023d8e4b1640c394777d34abaff1df8f"
REMOVE = {SURPLUS.lower(), NONSTD.lower()}


def abort(msg):
    print(f"ABORT: {msg}")
    raise SystemExit(1)


def main():
    # Gate
    caps = json.loads((HERE / "caps.json").read_text())
    if not caps.get("kill_switch"):
        abort("kill_switch not true — refusing to modify discovery state")

    # --- discovered.jsonl (JSONL, drop any line referencing either token) ---
    dj = HERE / "discovered.jsonl"
    before_lines = 0
    kept = []
    if dj.exists():
        for raw in dj.read_text().splitlines():
            if not raw.strip():
                continue
            before_lines += 1
            low = raw.lower()
            if any(tok in low for tok in REMOVE):
                continue  # drop
            kept.append(raw)
    removed_dj = before_lines - len(kept)
    backup = dj.with_suffix(f".jsonl.bak.{TS}")
    shutil.copy(dj, backup)
    atomic_jsonl_strings_write(str(dj), kept)

    # --- scout_state.json (dict keyed by token address) ---
    ss = HERE / "scout_state.json"
    removed_ss = 0
    if ss.exists():
        data = json.loads(ss.read_text())
        backup_ss = ss.with_suffix(f".json.bak.{TS}")
        shutil.copy(ss, backup_ss)
        for tok in list(data.keys()):
            if tok.lower() in REMOVE:
                del data[tok]
                removed_ss += 1
        ss.write_text(json.dumps(data, indent=2))

    # --- blocked_tokens.json (prevent re-discovery) ---
    bt = HERE / "blocked_tokens.json"
    existing = json.loads(bt.read_text()) if bt.exists() else {"tokens": []}
    if "tokens" not in existing or not isinstance(existing["tokens"], list):
        existing["tokens"] = []
    cur = {(t.get("address") or "").lower(): t for t in existing["tokens"]}
    for addr, reason in [
        (SURPLUS, "v4 hook dust — unsellable; dropped from watchlists per audit"),
        (NONSTD, "nonstandard ERC20 — approve() silent no-op; fail-closed #2 class"),
    ]:
        cur[addr.lower()] = {"address": addr, "reason": reason, "added": TS}
    existing["tokens"] = list(cur.values())
    bt.write_text(json.dumps(existing, indent=2))

    print(f"discovered.jsonl: removed {removed_dj} lines ({before_lines} -> {len(kept)})")
    print(f"scout_state.json: removed {removed_ss} keys")
    print(f"blocked_tokens.json: {len(existing['tokens'])} entries")
    print(f"backups: {backup.name}, {backup_ss.name}")
    print("swap.js / caps.json / shadow_book.json / risk_state.json untouched")


if __name__ == "__main__":
    main()
