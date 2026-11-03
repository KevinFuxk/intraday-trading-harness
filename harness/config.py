"""
config.py — every tunable in one place
======================================
The values below are ILLUSTRATIVE defaults for the public harness. They are not the
parameters any production strategy ran with, and nothing here encodes an edge.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

LIVE_PORTS = {4001, 7496}          # IB Gateway live / TWS live — the tools refuse these
PAPER_PORT = 4002                  # IB Gateway paper


@dataclass
class EngineConfig:
    arm: bool = False              # False = SHADOW: narrate what it would do, place nothing
    replay: bool = False           # walk a completed session through the live pipeline
    risk: float = 0.005            # fraction of sizing equity risked per trade
    tp: float = 2.0                # take-profit, in R multiples of the entry-to-stop distance
    minstop: float = 0.50          # skip setups whose stop is thinner than this % of price
    max_positions: int = 3         # concurrent symbols this strategy may hold or have armed
    base: float = 0.0              # >0: size a VIRTUAL account of this size on the real one
    eod: time = time(15, 50)       # flatten time (ET); nothing is armed after it


@dataclass(frozen=True)
class UniverseRules:
    """Applied to every ticker the watchlist hands the engine. Every drop is recorded with
    its reason; nothing is silently filtered."""
    min_price: float = 5.0                                   # judged on the SESSION'S OPEN
    allowed_types: frozenset = frozenset({"COMMON", "ADR"})  # broker stockType
    type_whitelist: frozenset = frozenset()                  # symbols exempt from the type rule only
    excluded_industries: frozenset = frozenset()             # broker industry classification
    excluded_categories: frozenset = frozenset()             # broker category classification


# Example of a stricter policy: a sector exclusion through the broker's own classification,
# and two ETFs admitted despite the common-stock rule. Illustrative values only.
EXAMPLE_STRICT_RULES = UniverseRules(
    min_price=8.0,
    type_whitelist=frozenset({"DIA", "IWM"}),
    excluded_industries=frozenset({"Utilities"}),
    excluded_categories=frozenset({"REITS"}),
)


@dataclass
class Paths:
    fills: str = "data/fills.csv"                  # every fill, with slippage vs the intended level
    brackets: str = "data/brackets.csv"            # every bracket placed: ref -> trigger / stop / target
    ledger: str = "data/ledger.csv"                # one row per closed trade, per variant
    universe: str = "data/universe_log.csv"        # every ticker, every day, tradable or why not
    watchlists: str = "data/watchlists"            # the point-in-time daily universe archive
    executions: str = "data/executions"            # the broker's executions, saved the same day
    cleanup: str = "data/cleanup_log.csv"          # leftover-position closes
    logs: str = "logs"
    extra: dict = field(default_factory=dict)


PATHS = Paths()
