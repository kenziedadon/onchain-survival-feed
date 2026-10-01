#!/usr/bin/env python3
"""shadow_book.py — turn skipped signals into free experiments.

When executor skips a candidate (timing filter, macro veto, cooldown, blocked
sell path, etc.) we log a SHADOW ENTRY: what we *would* have bought, at what
price. A trailing checker then closes shadow positions using the same exit
logic horizon (max_position_age_hours or +3x/-1x ATR approximated as
+15%/-8% bands) and records hypothetical PnL in telemetry.jsonl.

Over time this answers the only question that matters: are our filters
throwing away edge? If shadow PnL of "unfavorable timing" entries is strongly
positive, the filter is too strict and we retune it.

CLI:
    python3 shadow_book.py open <token_addr> <name> <price_usd> <reason>   # called by executor hooks
    python3 shadow_book.py check                                           # close matured shadows
    python3 shadow_book.py report                                          # shadow PnL by skip reason
"""
import json
import os
import sys
from datetime import datetime, timezone

BOT = os.path.dirname(os.path.abspath(__file__))
SHADOW_FILE = os.path.join(BOT, "shadow_book.json")
TELEMETRY_LEDGER = os.path.join(BOT, "telemetry.jsonl")

# Hypothetical exit bands standing in for live ATR exits (conservative).
TP_PCT = 0.15   # take profit +15%
SL_PCT = -0.08  # stop -8%
MAX_AGE_H = 12  # match caps max_position_age_hours


def _load():
    if os.path.exists(SHADOW_FILE):
        try:
            return json.load(open(SHADOW_FILE))
        except Exception:
            pass
    return {"open": {}, "closed": []}


def _save(book):
    json.dump(book, open(SHADOW_FILE, "w"), indent=1)


def _log(rec):
    with open(TELEMETRY_LEDGER, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def open_shadow(token, name, price_usd, reason, amount_eth=0.0005):
    book = _load()
    book["open"][token] = {
        "name": name,
        "entry_price_usd": price_usd,
        "reason": reason,
        "amount_eth": amount_eth,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _save(book)
    _log({"ts": datetime.now(timezone.utc).isoformat(), "kind": "shadow_entry",
          "token": token, "name": name, "reason": reason,
          "price_usd": price_usd, "meta": {"amount_eth": amount_eth}})
    print(f"SHADOW OPEN: {name} @ ${price_usd} ({reason})")


def check(price_lookup):
    """Close matured shadow positions. price_lookup(token)->usd price."""
    import time as _t
    from datetime import timedelta
    book = _load()
    now = datetime.now(timezone.utc)
    still_open = {}
    for token, pos in list(book["open"].items()):
        ts = datetime.fromisoformat(pos["ts"])
        age_h = (now - ts).total_seconds() / 3600
        px = price_lookup(token)
        if px is None:
            if age_h > MAX_AGE_H + 6:
                # stale beyond grace: close flat
                _close(book, token, pos, pos["entry_price_usd"], "stale_flat")
            else:
                still_open[token] = pos
            continue
        entry = pos["entry_price_usd"]
        chg = (px - entry) / entry if entry else 0.0
        if chg >= TP_PCT:
            _close(book, token, pos, px, "tp")
        elif chg <= SL_PCT:
            _close(book, token, pos, px, "sl")
        elif age_h > MAX_AGE_H:
            _close(book, token, pos, px, "time_exit")
        else:
            still_open[token] = pos
    book["open"] = still_open
    _save(book)


def _close(book, token, pos, exit_px, how):
    entry = pos["entry_price_usd"]
    pnl_pct = (exit_px - entry) / entry if entry else 0.0
    rec = dict(pos)
    rec.update({"token": token, "exit_price_usd": exit_px, "how": how,
                "pnl_pct": round(pnl_pct, 4),
                "closed_ts": datetime.now(timezone.utc).isoformat()})
    book["closed"].append(rec)
    _log({"ts": rec["closed_ts"], "kind": "shadow_exit", "token": token,
          "name": pos.get("name"), "reason": pos.get("reason"),
          "price_usd": exit_px,
          "meta": {"pnl_pct": rec["pnl_pct"], "how": how}})


def gecko_price(token):
    import urllib.request, json as _json
    url = f"https://api.geckoterminal.com/api/v2/networks/base/tokens/{token}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = _json.load(r)
        p = data["data"]["attributes"]["price_usd"]
        return float(p) if p else None
    except Exception:
        return None


def report():
    book = _load()
    by_reason = {}
    wins = losses = 0
    total_pnl_eth = 0.0
    for c in book["closed"]:
        r = c.get("reason", "?")
        amt = float(c.get("amount_eth", 0) or 0)
        pnl_eth = amt * c.get("pnl_pct", 0)
        by_reason.setdefault(r, []).append(c.get("pnl_pct", 0))
        total_pnl_eth += pnl_eth
        if c.get("pnl_pct", 0) > 0:
            wins += 1
        else:
            losses += 1
    out = {"closed": len(book["closed"]), "open": len(book["open"]),
           "wins": wins, "losses": losses,
           "shadow_pnl_eth": round(total_pnl_eth, 6), "by_reason": {}}
    for r, ps in sorted(by_reason.items()):
        n = len(ps)
        out["by_reason"][r] = {"n": n, "avg_pnl_pct": round(sum(ps) / n, 4),
                               "win_rate": round(sum(1 for p in ps if p > 0) / n, 2)}
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "open" and len(sys.argv) >= 6:
        open_shadow(sys.argv[2], sys.argv[3], float(sys.argv[4]), sys.argv[5])
    elif cmd == "check":
        book = _load()
        tokens = {t: gecko_price(t) for t in book["open"]}
        check(lambda t: tokens.get(t))
        print(f"checked {len(tokens)} shadow positions")
    elif cmd == "report":
        report()
    else:
        print(__doc__)
