"""
strategy.py — the contract between a strategy and the engine
============================================================
The engine knows nothing about chart patterns. It knows SETUPS: things that are forming,
that may at some point cross a trigger, and that carry a protective stop. A strategy is
anything that can answer one question on every closed bar:

    "Given everything printed so far today, what setups exist and what state is each in?"

    FORMING   valid so far, not triggered. `trigger` / `stop` are live values. The engine
              may PRE-ARM a resting buy-stop once the next bar index reaches `earliest`.
    ENTERED   bar `entry_bar` crossed the trigger.
    DEAD      invalidated (too deep, too old, broken — the strategy decides).

THE CONTRACT (harness.parity checks every rule mechanically; tests/ runs it):

  1. NO LOOK-AHEAD. scan(bars.prefix(n)) may read bars[:n] and nothing else.
  2. FINALITY. Once a setup is ENTERED or DEAD it stays so on every longer prefix.
     Printed bars cannot un-print, so neither can their verdicts.
  3. PRE-ARM TIMING. earliest > key: there is always at least one CLOSED bar between a
     setup becoming known and the first bar on which it may fill — the window in which
     the resting order is placed. The engine arms only when len(bars) >= earliest, so a
     resting order can never fill on a bar the backtest would have rejected.
  4. REFEREE PARITY. detect() is an INDEPENDENT batch implementation run over the whole
     session. Every entry it reports must be the bar at which scan() first reported that
     setup ENTERED. The live engine prints a referee line for every detector entry and
     says whether the pre-armer caught it — two implementations, one truth.
  5. STOPS ONLY TIGHTEN DOWNWARD WHILE FORMING. If a FORMING setup's stop moves DOWN the
     engine re-sizes and re-places the resting bracket. An upward move is ignored: the
     risk a bracket was sized for is never silently widened after the fact.

`memo` is an optional per-(symbol, timeframe, session) dict owned by the engine. A strategy
may cache work in it (results for setups that are already final), provided the output is
identical to a memo-less scan — the private strategies use this to turn an O(n^2)
whole-session rescan into near-O(n), with an equivalence test pinning the two together.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .bars import Bars

FORMING, ENTERED, DEAD = "forming", "entered", "dead"


@dataclass(frozen=True)
class Setup:
    key: int                       # stable identity within the session (e.g. the defining bar's index)
    state: str                     # FORMING | ENTERED | DEAD
    trigger: float                 # the buy-stop level
    stop: float                    # the protective stop level
    earliest: int                  # first bar index on which a resting order may legally fill
    entry_bar: int | None = None   # the bar that crossed the trigger (state == ENTERED)
    label: str = ""                # free-form tag carried into the log


@dataclass(frozen=True)
class Entry:
    bar: int                       # the bar index on which the entry happened
    price: float                   # the entry price the batch detector books


class Strategy(Protocol):
    name: str                      # short, unique: it namespaces every order ref this strategy sends

    def scan(self, bars: Bars, memo: dict | None = None) -> dict[int, Setup]:
        """Every setup visible in `bars` and its CURRENT state, keyed by Setup.key."""
        ...

    def detect(self, bars: Bars) -> list[Entry]:
        """Independent batch implementation: every entry in a COMPLETE session."""
        ...
