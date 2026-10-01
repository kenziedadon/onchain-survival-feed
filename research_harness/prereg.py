"""Preregistration for the WO4 research harness.

The failure this prevents: running a strategy, seeing the result, then editing
params until the run looks good. The spec is registered BEFORE the run, hashed,
and the runner refuses anything unregistered. Changing a spec requires a new
registration, because that is a new hypothesis.

Two hashes are recorded, not one:
  spec_hash       — the experiment definition (params, data, split, costs, gates)
  strategy_hash   — the code underneath it

Registering the spec while quietly editing the strategy file is the same class
of cheat, so both are stamped into the artifact and the ledger.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

SPECS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "specs")
REGISTRY = os.path.join(SPECS_DIR, "registry.jsonl")
DATA_PINS = os.path.join(SPECS_DIR, "data_pins.jsonl")

#: Permitted analysis tests. WO4 specifies a ONE-SIDED test — the hypothesis
#: is "strategy return > benchmark", never "they differ". `welch_t` (two-sided)
#: stays available for a spec that genuinely tests a difference, but a spec
#: that does not say which one it wants is refused rather than defaulted:
#: silently defaulting would put a statistic in the artifact that the
#: preregistration never described.
ALLOWED_TESTS = frozenset({"welch_t", "welch_t_one_sided"})

#: Required keys. A spec missing any of these is not a valid experiment.
REQUIRED = (
    "strategy_id",
    "strategy_version",
    "params",
    "data",
    "split",
    "costs",
    "gates",
    "analysis",
)


class PreregError(RuntimeError):
    """A spec cannot be registered or executed. Hard stop, nothing written."""


def _canonical(obj: Any) -> str:
    """Deterministic serialization: same content -> same bytes -> same hash,
    regardless of file formatting or key order."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def spec_hash(spec: dict) -> str:
    """SHA-256 of the canonical spec. Full 64 hex; short form for filenames."""
    h = hashlib.sha256(_canonical(spec).encode("utf-8")).hexdigest()
    return h


def short(h: str, n: int = 12) -> str:
    return h[:n]


def strategy_hash(strategy_id: str) -> str:
    """SHA-256 of the strategy module source, so code drift is visible."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "strategies", f"{strategy_id}.py")
    if not os.path.exists(path):
        raise PreregError(f"strategy module not found: {path}")
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def validate_spec(spec: dict) -> None:
    missing = [k for k in REQUIRED if k not in spec]
    if missing:
        raise PreregError(f"spec missing required keys: {missing}")
    if not isinstance(spec["params"], dict):
        raise PreregError("spec.params must be an object")
    tf = spec["split"].get("train_frac")
    if not isinstance(tf, (int, float)) or not 0.1 <= tf <= 0.9:
        raise PreregError(
            f"split.train_frac must be in [0.1, 0.9], got {tf!r}")
    if spec["analysis"].get("test") not in ALLOWED_TESTS:
        raise PreregError(
            f"unsupported analysis test: {spec['analysis'].get('test')!r} "
            f"(allowed: {sorted(ALLOWED_TESTS)})")
    alpha = spec["analysis"].get("alpha", 0.05)
    if not 0 < alpha < 1:
        raise PreregError(f"analysis.alpha must be in (0,1), got {alpha!r}")


def register(spec: dict, who: str = "unknown", note: str = "") -> dict:
    """Register a spec. Append-only ledger; re-registering identical content
    is a no-op, re-registering CHANGED content requires the old hash to be
    acknowledged via `supersedes`."""
    validate_spec(spec)
    h = spec_hash(spec)
    os.makedirs(SPECS_DIR, exist_ok=True)

    path = os.path.join(SPECS_DIR, f"{spec['strategy_id']}.{short(h)}.json")
    entry = {
        "spec_hash": h,
        "short": short(h),
        "strategy_hash": strategy_hash(spec["strategy_id"]),
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "who": who,
        "note": note,
    }
    if os.path.exists(path):
        entry["status"] = "already_registered"
    else:
        entry["status"] = "registered"
    with open(path, "w") as fh:
        json.dump(spec, fh, indent=2, sort_keys=True)
    with open(REGISTRY, "a") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def _read_jsonl(path: str) -> list:
    """Parse a JSONL file, skipping blanks. Missing file -> empty list."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def ledger() -> list[dict]:
    return _read_jsonl(REGISTRY)


def is_registered(h: str) -> bool:
    return any(e["spec_hash"] == h for e in ledger())


def load_spec(h: str) -> dict:
    """Load a spec by full or short hash, via the ledger."""
    entries = [e for e in ledger() if e["short"] == h[:12] or e["spec_hash"] == h]
    if not entries:
        raise PreregError(f"spec {h!r} not found in ledger")
    short_h = entries[-1]["short"]
    matches = [fn for fn in os.listdir(SPECS_DIR)
               if fn.endswith(".json") and fn.split(".")[-2] == short_h]
    if not matches:
        raise PreregError(f"ledger references {short_h} but no spec file exists")
    with open(os.path.join(SPECS_DIR, matches[0])) as fh:
        return json.load(fh)


def require_registered(spec: dict) -> str:
    """The refusal gate. Returns the hash, or raises. Nothing runs if this
    raises — the caller must not catch it and proceed."""
    h = spec_hash(spec)
    if not is_registered(h):
        raise PreregError(
            f"spec {short(h)} is NOT registered — refusing to run. "
            f"A run that was not preregistered cannot be cited as evidence. "
            f"Register it first: prereg.register(spec).")
    return h


def provenance(spec: dict, spec_h: str, infrastructure_validation: bool) -> dict:
    """Stamped at run START, before any data fetch."""
    return {
        "spec_hash": spec_h,
        "spec_short": short(spec_h),
        "strategy_hash": strategy_hash(spec["strategy_id"]),
        "registered_before_run": True,
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        # True only for the very first harness-validation run, which predates
        # preregistration enforcement. Such a run cannot be strategy evidence.
        "infrastructure_validation": bool(infrastructure_validation),
        "preregistered": not infrastructure_validation,
    }


# ---------------------------------------------------------------------------
# Data pinning (WO4 review finding 3)
#
# The hole this closes: the spec pinned WHERE to fetch from (source, interval,
# limit) but never WHAT came back. GeckoTerminal returns a rolling trailing
# window, so the same registered spec re-run tomorrow silently evaluates a
# different 300 hours and still carries an identical spec_hash. Nothing in the
# artifact would reveal it. That makes re-run-until-it-looks-good legal while
# looking fully preregistered — preregistration with nothing pinned is theater.
#
# Fix: hash the fetched bars themselves and pin that hash, per spec, at
# registration time. A later run whose bars do not hash to the pinned value is
# REFUSED, not re-run. Rolling-window drift becomes a visible stop.
# ---------------------------------------------------------------------------

def data_hash(candles: list, pool_address: str = "") -> str:
    """SHA-256 over the canonical form of the fetched bars.

    Pool address is inside the hash so the same bar set attributed to a
    different pool cannot satisfy a pin. Rounded to 8dp because the upstream
    API returns floats whose repr can differ without the data differing.

    `ts` falls back to the bar's position when absent: the harness only needs
    timestamps for ordering, which the sequence already guarantees, and a pin
    must not become uncomputable for synthetic or pre-stamped bar sets that
    carry OHLCV without an explicit timestamp.
    """
    payload = {
        "pool": (pool_address or "").lower(),
        "bars": [
            [int(c["ts"]) if c.get("ts") is not None else i,
             round(float(c["o"]), 8), round(float(c["h"]), 8),
             round(float(c["l"]), 8), round(float(c["c"]), 8),
             round(float(c["v"]), 8)]
            for i, c in enumerate(candles)
        ],
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def data_window(candles: list) -> dict:
    """First/last bar timestamps — the human-readable half of the pin."""
    if not candles:
        return {"bars": 0, "start_ts": None, "end_ts": None}
    first, last = candles[0], candles[-1]
    return {"bars": len(candles),
            "start_ts": int(first["ts"]) if first.get("ts") is not None else 0,
            "end_ts": int(last["ts"]) if last.get("ts") is not None else 0}


def pin_data(spec: dict, spec_h: str, candles: list,
             pool_address: str = "", who: str = "unknown") -> dict:
    """Record the exact bar set this spec is allowed to be evaluated on.

    Called at registration, BEFORE the first run. Append-only, like the spec
    ledger: re-pinning the same content is a no-op, re-pinning different
    content requires `supersedes` naming the prior hash.
    """
    pool_key = (pool_address or "").lower()
    entry = {
        "spec_hash": spec_h,
        "spec_short": short(spec_h),
        "pool": pool_key,
        "data_hash": data_hash(candles, pool_address),
        "window": data_window(candles),
        "pinned_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "who": who,
    }
    # Scoped to (spec, pool). A spec pins one bar set PER POOL — the universe
    # has several, so comparing pool B's bars against pool A's pin would make
    # every multi-pool run raise a false conflict on its second pool.
    prior = [e for e in _read_jsonl(DATA_PINS)
             if e["spec_hash"] == spec_h and e.get("pool", "") == pool_key]
    if prior:
        if prior[-1]["data_hash"] == entry["data_hash"]:
            entry["status"] = "already_pinned"
        else:
            entry["status"] = "conflict"
            entry["supersedes"] = prior[-1]["data_hash"]
    else:
        entry["status"] = "pinned"
    if entry["status"] == "conflict":
        raise PreregError(
            f"data pin conflict for spec {short(spec_h)} pool {pool_key or '(none)'}: "
            f"pinned {short(prior[-1]['data_hash'])} covering bars "
            f"{prior[-1]['window']}, fetched {short(entry['data_hash'])} "
            f"covering {entry['window']}. Re-pinning to a different window is "
            f"a new experiment: register a new spec, do not overwrite the pin.")
    os.makedirs(SPECS_DIR, exist_ok=True)
    with open(DATA_PINS, "a") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def require_pinned_data(spec_h: str, candles: list,
                        pool_address: str = "") -> dict:
    """The data refusal gate. Returns the pin, or raises. Nothing runs if it
    raises — the caller must not catch it and proceed.

    Raises when:
      * no pin exists for this spec (preregistration did not pin any data), or
      * the fetched bars do not hash to the pinned value (window drifted).
    """
    all_pins = [e for e in _read_jsonl(DATA_PINS) if e["spec_hash"] == spec_h]
    if not all_pins:
        raise PreregError(
            f"spec {short(spec_h)} has NO pinned data — refusing to run. "
            f"A preregistration that pins the fetch parameters but not the "
            f"fetched bars does not pin the experiment: the same spec on a "
            f"later day evaluates a different window and still looks "
            f"registered. Pin first: prereg.pin_data(spec, spec_h, candles).")

    # Select THIS pool's pin, not the newest one overall. A spec pins one bar
    # set per pool, so an unpinned pool must be refused outright rather than
    # silently validated against whichever pool happened to be pinned last.
    key = (pool_address or "").lower()
    matches = [e for e in all_pins if e.get("pool", "") == key]
    if not matches:
        raise PreregError(
            f"spec {short(spec_h)} has no data pin for pool {key or '(none)'} "
            f"— refusing to run. Pinned pools: "
            f"{sorted(e.get('pool','') for e in all_pins)}. Register the whole "
            f"universe's data before evaluating any of it.")
    pin = matches[-1]
    got = data_hash(candles, pool_address)
    if got != pin["data_hash"]:
        w, g = data_window(candles), pin["window"]
        raise PreregError(
            f"DATA PIN MISMATCH for spec {short(spec_h)}: pinned "
            f"{short(pin['data_hash'])} over {w['bars']} bars "
            f"[{w['start_ts']}..{w['end_ts']}]; fetched {short(got)} over "
            f"{g['bars']} bars [{g['start_ts']}..{g['end_ts']}]. The upstream "
            f"window rolled. Refusing rather than evaluating different data "
            f"under a registered spec — register a new spec for the new window.")
    return pin