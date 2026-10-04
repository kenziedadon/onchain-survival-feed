#!/usr/bin/env python3
"""
Macro regime filter for Base trading bot.

Monitors US 10Y Treasury yield and Base chain TVL to determine
risk-on / risk-off regime. When enabled in caps.json, blocks trades
during risk-off periods.

Data sources (free, no API key required):
- US 10Y Yield: FRED CSV (https://fred.stlouisfed.org/graph/fredgraph.csv?id=GS10)
- Base TVL: DeFi Llama API (https://api.llama.fi/v2/historicalChainTvl/Base)
"""

import json
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# Default paths
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CAPS = BASE_DIR / "caps.json"

# Default snapshot path (writer heartbeat + consumer freshness source)
DEFAULT_SNAPSHOT = Path.home() / ".hermes" / "shared" / "macro_regime.json"

#: Staleness budget. 8h = two missed 4h writer intervals. McKenzie: keep 8h
#: while advisory/disarmed; tighten to 4h only if this filter ever hard-gates
#: while armed (his call, not the module's).
DEFAULT_MAX_STALE_HOURS = 8.0


def _load_snapshot(path: Path) -> dict:
    """Read + structurally validate the snapshot. Raises ValueError on any
    failure so the caller can turn it into a fail-closed block."""
    if not path.exists():
        raise ValueError(f"regime snapshot missing ({path})")
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"regime snapshot unreadable ({e})") from e
    if not isinstance(payload, dict):
        raise ValueError("regime snapshot is not a JSON object")
    if "timestamp" not in payload:
        raise ValueError("regime snapshot has no timestamp")
    try:
        parsed = datetime.fromisoformat(str(payload["timestamp"]))
    except ValueError as e:
        raise ValueError(
            f"regime snapshot timestamp unparseable ({payload['timestamp']!r})"
        ) from e
    # An offset-aware timestamp is a MACRO BLOCK, not a pass. Mixing aware and
    # naive datetimes raises TypeError on subtraction in
    # check_snapshot_freshness, which is NOT caught by the ValueError path
    # there -- it would escape as an unhandled exception instead of a clean
    # fail-closed verdict. Reject it here so it lands on the ValueError path.
    if parsed.tzinfo is not None:
        raise ValueError("regime snapshot timestamp must be naive")
    return payload


def check_snapshot_freshness(
    path: str | Path,
    max_age_hours: float = DEFAULT_MAX_STALE_HOURS,
    now: datetime | None = None,
    clock_skew_tolerance_s: float = 300.0,
) -> tuple[bool, str]:
    """Fail-closed freshness check. READ-ONLY -- writes nothing, ever.

    Returns (ok, reason). ok=False when the snapshot is missing, unreadable,
    malformed, timestamp-less, unparseable, future-dated, or older than
    max_age_hours. Every one of those states means the caller MUST NOT
    return ALLOW: unknown macro never permits a trade.

    Future-dating is a BLOCK, not a pass. A timestamp ahead of the read
    clock would otherwise produce a negative age, which sails past a
    `> max_age_hours` test and launders a bad or forged file into an
    indefinite pass. A small tolerance covers ordinary NTP drift only.
    """
    path = Path(path)
    if now is None:
        now = datetime.now()

    try:
        payload = _load_snapshot(path)
    except ValueError as e:
        return False, f"MACRO BLOCK: {e}"

    written = datetime.fromisoformat(str(payload["timestamp"]))
    age_s = (now - written).total_seconds()

    if age_s < -clock_skew_tolerance_s:
        return False, (
            f"MACRO BLOCK: regime snapshot timestamp is in the future "
            f"({-age_s:.0f}s ahead of read clock, written "
            f"{written.isoformat(timespec='seconds')})"
        )

    age_h = age_s / 3600.0
    if age_h > max_age_hours:
        return False, (
            f"MACRO BLOCK: regime snapshot stale ({age_h:.1f}h > "
            f"{max_age_hours:g}h, written "
            f"{written.isoformat(timespec='seconds')})"
        )

    return True, f"regime snapshot fresh ({age_h:.1f}h old)"


def collect_regime_snapshot(
    caps_path: str | Path | None = None,
) -> dict:
    """Fetch live macro inputs and return the snapshot payload.

    Contains NO freshness gate and NO verdict logic. This is the data half
    of the heartbeat, factored out so the writer can run it without ever
    consulting -- or being blocked by -- the consumer's staleness check.
    """
    if caps_path is None:
        caps_path = DEFAULT_CAPS

    try:
        caps = json.loads(Path(caps_path).read_text())
    except json.JSONDecodeError:
        raise ValueError("caps.json unreadable") from None

    macro_cfg = caps.get("macro_regime", {})
    lookback = macro_cfg.get("lookback_days", DEFAULT_THRESHOLDS["lookback_days"])
    thresholds = {**DEFAULT_THRESHOLDS, **macro_cfg}

    yield_data = fetch_us10_yahoo(lookback_days=lookback)
    if not yield_data:
        yield_data = fetch_fred_csv("GS10", lookback_days=lookback)
    tvl_data = fetch_base_tvl(lookback_days=lookback)

    if not yield_data or not tvl_data:
        raise ValueError("insufficient macro data")

    yield_change = pct_change(yield_data, "value")
    tvl_change = pct_change(tvl_data, "tvl")

    return {
        "regime": evaluate_regime(yield_change, tvl_change, thresholds),
        "yield_10y_current": yield_data[-1]["value"],
        "yield_10y_change_pct": round(yield_change * 100, 4),
        "base_tvl_change_pct": round(tvl_change * 100, 4),
        "thresholds": thresholds,
    }


def write_regime_snapshot(
    payload: dict,
    path: str | Path,
    now: datetime | None = None,
) -> Path:
    """Write the snapshot UNCONDITIONALLY. Never reads the freshness check.

    The embedded timestamp IS the liveness heartbeat. Write-on-change is
    indistinguishable from a dead writer: if the file were only rewritten
    when the regime flips, a crashed cron and a stable market would look
    identical to the consumer. So every invocation refreshes it.

    The embedded timestamp is the write time, keeping mtime and the embedded
    clock consistent by construction (the 09-30 disagreement). Atomic replace
    so a concurrent reader never sees a half-written file.
    """
    path = Path(path)
    if now is None:
        now = datetime.now()
    payload = dict(payload)
    payload["timestamp"] = now.isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)
    return path


def run_macro_regime_writer(
    caps_path: str | Path | None = None,
    snapshot_path: str | Path | None = None,
) -> tuple[bool, str]:
    """UNGATED heartbeat entry point. This is what the 4h cron calls.

    Deliberately independent of check_snapshot_freshness(): a stale or missing
    file must never stop the writer from running, or a dead cron could never
    heal itself. The only thing that can prevent a heartbeat is the writer
    itself failing, which is the honest failure signal.

    Writes on every call regardless of whether the regime changed.
    """
    if caps_path is None:
        caps_path = DEFAULT_CAPS

    if snapshot_path is None:
        try:
            caps = json.loads(Path(caps_path).read_text())
            snapshot_path = caps.get("macro_regime", {}).get(
                "snapshot_path", DEFAULT_SNAPSHOT)
        except (json.JSONDecodeError, OSError):
            snapshot_path = DEFAULT_SNAPSHOT

    try:
        payload = collect_regime_snapshot(caps_path)
    except ValueError as e:
        return False, f"heartbeat NOT written: {e}"
    except Exception as e:  # network/parse faults must not kill the cron
        return False, f"heartbeat NOT written: data fetch failed ({e})"

    written = write_regime_snapshot(payload, snapshot_path)
    return True, f"heartbeat written to {written}"


# Default thresholds
DEFAULT_THRESHOLDS = {
    "yield_drop_pct": -0.05,   # yields down 5%+ = risk-on trigger
    "yield_rise_pct": 0.05,    # yields up 5%+ = risk-off trigger
    "tvl_rise_pct": 0.02,      # TVL up 2%+ = risk-on confirmation
    "tvl_drop_pct": -0.02,     # TVL down 2%+ = risk-off confirmation
    "lookback_days": 30,       # default lookback window
}


def fetch_us10_yahoo(lookback_days: int = 30) -> list[dict]:
    """
    Fetch US 10Y yield from Yahoo Finance ^TNX.
    Returns list of {date: str, value: float} sorted by date.
    Yahoo returns percent values already (e.g. 4.65 for 4.65%).
    """
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX?interval=1d&range=30d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    
    result = data["chart"]["result"][0]
    timestamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    rows = []
    
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        row_date = datetime.utcfromtimestamp(ts)
        if row_date >= cutoff:
            rows.append({
                "date": row_date.strftime("%Y-%m-%d"),
                "value": float(close),
            })
    
    return sorted(rows, key=lambda x: x["date"])


def fetch_fred_csv(series_id: str, lookback_days: int = 30) -> list[dict]:
    """
    Fetch historical data from FRED as CSV.
    Returns list of {date: str, value: float} sorted by date.
    NOTE: FRED monthly series lag 1-2 months; prefer Yahoo Finance ^TNX.
    """
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    
    with urllib.request.urlopen(req, timeout=20) as r:
        text = r.read().decode("utf-8", errors="ignore")
    
    cutoff = datetime.now() - timedelta(days=lookback_days)
    rows = []
    
    for line in text.strip().split("\n")[1:]:  # skip header
        parts = line.split(",")
        if len(parts) != 2:
            continue
        date_str, value_str = parts
        try:
            value = float(value_str)
            row_date = datetime.strptime(date_str, "%Y-%m-%d")
            if row_date >= cutoff:
                rows.append({"date": date_str, "value": value})
        except (ValueError, TypeError):
            continue
    
    return sorted(rows, key=lambda x: x["date"])


def fetch_base_tvl(lookback_days: int = 30) -> list[dict]:
    """
    Fetch Base chain historical TVL from DeFi Llama.
    Returns list of {date: str, tvl: float} sorted by date.
    """
    url = "https://api.llama.fi/v2/historicalChainTvl/Base"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    
    cutoff = datetime.now() - timedelta(days=lookback_days)
    rows = []
    
    # DeFi Llama returns list of {date, tvl} objects
    if isinstance(data, list):
        for entry in data:
            try:
                tvl = float(entry.get("tvl", 0))
                timestamp = entry.get("date", 0)
                if isinstance(timestamp, (int, float)):
                    row_date = datetime.utcfromtimestamp(timestamp)
                else:
                    row_date = datetime.strptime(str(timestamp), "%Y-%m-%d")
                
                if row_date >= cutoff:
                    rows.append({
                        "date": row_date.strftime("%Y-%m-%d"),
                        "tvl": tvl,
                    })
            except (ValueError, TypeError, OSError):
                continue
    
    return sorted(rows, key=lambda x: x["date"])


def pct_change(series: list[dict], key: str) -> float:
    """Compute percent change from first to last data point."""
    if len(series) < 2:
        return 0.0
    first = series[0][key]
    last = series[-1][key]
    if first == 0:
        return 0.0
    return (last - first) / first


def evaluate_regime(
    yield_change_pct: float,
    tvl_change_pct: float,
    thresholds: dict | None = None,
) -> str:
    """
    Evaluate macro regime based on yield and TVL changes.
    
    Returns:
        'risk_on'   - yields falling + TVL rising
        'risk_off'  - yields rising + TVL falling
        'neutral'   - mixed signals
    """
    if thresholds is None:
        thresholds = DEFAULT_THRESHOLDS
    
    yield_drop = thresholds.get("yield_drop_pct", -0.05)
    yield_rise = thresholds.get("yield_rise_pct", 0.05)
    tvl_rise = thresholds.get("tvl_rise_pct", 0.02)
    tvl_drop = thresholds.get("tvl_drop_pct", -0.02)
    
    yields_falling = yield_change_pct <= yield_drop
    yields_rising = yield_change_pct >= yield_rise
    tvl_rising = tvl_change_pct >= tvl_rise
    tvl_falling = tvl_change_pct <= tvl_drop
    
    if yields_falling and tvl_rising:
        return "risk_on"
    elif yields_rising and tvl_falling:
        return "risk_off"
    else:
        return "neutral"


def evaluate_macro_regime(
    caps_path: str | Path | None = None,
) -> tuple[bool, str]:
    """Main entry point for the executor/risk module. READ-ONLY CONSUMER.

    Writes nothing. The heartbeat belongs to run_macro_regime_writer(), which
    the 4h cron calls independently -- keeping the writer off this path is what
    lets a dead cron fail closed AND a recovered cron heal itself. If this
    function wrote, the gate would gate its own only writer and the block
    would be permanent once the file went stale.

    Returns:
        (allowed: bool, reason: str)
        - allowed=True means trades can proceed
        - allowed=False means macro is risk-off, stale, or unknown; block
    """
    if caps_path is None:
        caps_path = DEFAULT_CAPS

    # Load caps
    try:
        caps = json.loads(Path(caps_path).read_text())
    except json.JSONDecodeError:
        return False, "MACRO BLOCK: caps.json unreadable"

    macro_cfg = caps.get("macro_regime", {})

    if not macro_cfg.get("enabled", False):
        return True, "MACRO SKIP: regime filter disabled in caps.json"

    # --- Fail-closed freshness. ALLOW is unreachable when the snapshot is
    # stale, missing, malformed, future-dated, or unknown. ---
    fresh, why = check_snapshot_freshness(
        macro_cfg.get("snapshot_path", DEFAULT_SNAPSHOT),
        max_age_hours=macro_cfg.get("max_stale_hours",
                                    DEFAULT_MAX_STALE_HOURS),
    )
    if not fresh:
        return False, why

    lookback = macro_cfg.get("lookback_days", DEFAULT_THRESHOLDS["lookback_days"])
    thresholds = {**DEFAULT_THRESHOLDS, **macro_cfg}

    # Fetch data - prefer Yahoo Finance ^TNX (daily), fall back to FRED (monthly)
    try:
        yield_data = fetch_us10_yahoo(lookback_days=lookback)
        if not yield_data:
            yield_data = fetch_fred_csv("GS10", lookback_days=lookback)
        tvl_data = fetch_base_tvl(lookback_days=lookback)
    except Exception as e:
        return False, f"MACRO BLOCK: data fetch failed ({e})"

    if not yield_data or not tvl_data:
        return False, "MACRO BLOCK: insufficient data"

    yield_change = pct_change(yield_data, "value")
    tvl_change = pct_change(tvl_data, "tvl")

    regime = evaluate_regime(yield_change, tvl_change, thresholds)

    if regime == "risk_off":
        return (
            False,
            f"MACRO BLOCK: risk-off regime "
            f"(US10Y {yield_change:+.1%}, Base TVL {tvl_change:+.1%})",
        )
    elif regime == "risk_on":
        return (
            True,
            f"MACRO PASS: risk-on regime "
            f"(US10Y {yield_change:+.1%}, Base TVL {tvl_change:+.1%})",
        )
    else:
        return (
            True,
            f"MACRO NEUTRAL: mixed signals "
            f"(US10Y {yield_change:+.1%}, Base TVL {tvl_change:+.1%})",
        )


def _main() -> int:
    """Cron entry point: run the heartbeat writer, then report the gate.

    Writer runs FIRST and ungated, so applying this never leaves the consumer
    staring at a missing file. Exit 0 on a written heartbeat; 1 on failure.
    """
    import argparse

    ap = argparse.ArgumentParser(description="macro regime heartbeat writer")
    ap.add_argument("--caps", default=str(DEFAULT_CAPS))
    ap.add_argument("--snapshot", default=None,
                    help="override snapshot path (default: from caps.json)")
    args = ap.parse_args()

    ok, why = run_macro_regime_writer(args.caps, args.snapshot)
    print(why)

    allowed, reason = evaluate_macro_regime(args.caps)
    print(f"consumer: allowed={allowed} | {reason}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
