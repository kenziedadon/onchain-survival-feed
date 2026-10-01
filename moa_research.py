#!/usr/bin/env python3
"""MoA research/synthesis layer for base-trend-bot.

Shells out to the Hermes MoA preset (nous-free: 6 free Portal models) for the
qualitative analysis the scout can't do locally -- signal-vs-noise verdicts,
watchlist curation, narrative risk flags. Non-interactive: uses
`hermes -m moa:nous-free -z "<prompt>"` (single-shot batch mode).

Kept separate from base_scout.py so the cron monitor stays dependency-free and
fast; MoA is opt-in via `--moa` and only fires when an alert actually triggers.
"""
import subprocess
import sys

MOA_PRESET = "moa:nous-free"


def ask_moa(prompt: str, timeout: int = 300) -> str:
    """Run a single research question through the MoA pool. Returns text."""
    cmd = ["hermes", "chat", "-q", prompt, "-m", MOA_PRESET, "-Q"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (res.stdout or res.stderr).strip()
        return out or "(MoA returned no output)"
    except subprocess.TimeoutExpired:
        return "(MoA timed out)"
    except FileNotFoundError:
        return "(hermes binary not found on PATH)"
    except Exception as e:  # noqa: BLE001 - surface any failure as text, never crash the scout
        return f"(MoA error: {e})"


def enrich_alerts(alerts: list[str]) -> str:
    """Synthesize a batch of scout alerts into one MoA verdict call."""
    if not alerts:
        return ""
    joined = "\n".join(f"- {a}" for a in alerts)
    prompt = (
        "The following Base token alerts just fired from an automated scout "
        "(volume spike >25%, liquidity drop >10%, or price breakout >15%).\n"
        "For EACH alert, give a one-line verdict: genuine trading signal or noise "
        "(wash volume, brand-new pool, single-whale move). Then list any tokens worth "
        "adding to the watchlist.\n"
        f"Alerts:\n{joined}"
    )
    return ask_moa(prompt)


if __name__ == "__main__":
    q = " ".join(sys.argv[1:])
    if not q:
        print("usage: moa_research.py <question>")
        sys.exit(1)
    print(ask_moa(q))
