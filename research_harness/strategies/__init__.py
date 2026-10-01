"""Strategy registry — discovery by STRATEGY_ID.

Unknown id is a HARD ERROR. There is no fallback strategy and no silent
default, because a run that quietly substituted a different strategy would
still produce a plausible-looking number.
"""
from __future__ import annotations

import importlib
import os

_REQUIRED = ("STRATEGY_ID", "STRATEGY_VERSION", "generate_signals",
             "min_history_bars", "exit_rule")


class UnknownStrategy(KeyError):
    pass


class InvalidStrategyModule(RuntimeError):
    pass


def _module(strategy_id: str):
    if not strategy_id.replace("_", "").replace("-", "").isalnum():
        raise UnknownStrategy(f"illegal strategy id: {strategy_id!r}")
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        f"{strategy_id}.py")
    if not os.path.exists(path):
        raise UnknownStrategy(
            f"no such strategy: {strategy_id!r}. "
            f"Known: {', '.join(available())}")
    mod = importlib.import_module(f"research_harness.strategies.{strategy_id}")
    missing = [a for a in _REQUIRED if not hasattr(mod, a)]
    if missing:
        raise InvalidStrategyModule(
            f"strategy {strategy_id!r} missing required attributes: {missing}")
    if mod.STRATEGY_ID != strategy_id:
        raise InvalidStrategyModule(
            f"module {strategy_id}.py declares STRATEGY_ID="
            f"{mod.STRATEGY_ID!r} — filename/id mismatch")
    return mod


def get(strategy_id: str):
    return _module(strategy_id)


def available() -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    return sorted(fn[:-3] for fn in os.listdir(here)
                  if fn.endswith(".py") and not fn.startswith("_"))


def version(strategy_id: str) -> str:
    return _module(strategy_id).STRATEGY_VERSION
