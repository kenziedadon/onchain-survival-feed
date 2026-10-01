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
    """
    Main entry point for the executor/risk module.
    
    Returns:
        (allowed: bool, reason: str)
        - allowed=True means trades can proceed
        - allowed=False means macro regime is risk-off; block trades
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


if __name__ == "__main__":
    # Standalone check
    allowed, reason = evaluate_macro_regime()
    print(reason)
    if not allowed:
        print("ACTION: Block new trades until regime improves")
    else:
        print("ACTION: Trades permitted")
