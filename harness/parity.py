"""
parity.py — prove a strategy honours the contract, mechanically
================================================================
A backtest scans a whole session at once. Live, bars arrive one at a time and every
decision is made on a CLOSED bar without seeing the future. This module replays a session
bar by bar through `scan()` — exactly as the engine will — and checks it against the
strategy's independent batch `detect()`:

    match     streaming reported the entry on the SAME bar the batch detector did
    lag       streaming saw it only later  -> the batch implementation peeked ahead
    phantom   streaming entered, batch never did
    missed    batch entered, streaming never did

plus the two invariants a live engine relies on:

    finality  an ENTERED / DEAD setup never changes state on a longer prefix
    timing    every setup's `earliest` is strictly after its key (a closed bar exists
              to place the resting order in)

`check(strategy, bars)` returns a report; `assert_contract` raises on any violation.
The private strategies pass the same harness — including on real sessions — before a
single paper order is sent.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .bars import Bars
from .strategy import ENTERED, DEAD


@dataclass
class ParityReport:
    bars: int
    batch_entries: int = 0
    matched: int = 0
    lagged: list = field(default_factory=list)       # (key, batch_bar, streaming_bar)
    phantom: list = field(default_factory=list)      # (key, streaming_bar)
    missed: list = field(default_factory=list)       # batch bars with no streaming entry
    finality: list = field(default_factory=list)     # (key, n, old_state, new_state)
    timing: list = field(default_factory=list)       # (key, earliest)

    @property
    def ok(self) -> bool:
        return not (self.lagged or self.phantom or self.missed or self.finality or self.timing)

    def summary(self) -> str:
        if self.ok:
            return (f"contract OK — {self.matched}/{self.batch_entries} batch entries reproduced "
                    f"bar-for-bar by the streaming scan over {self.bars} bars")
        return (f"CONTRACT VIOLATED — matched {self.matched}/{self.batch_entries}; lag {self.lagged}; "
                f"phantom {self.phantom}; missed {self.missed}; finality {self.finality}; timing {self.timing}")


def check(strategy, bars: Bars, use_memo: bool = True) -> ParityReport:
    rep = ParityReport(bars=len(bars))
    memo = {} if use_memo else None
    first_entry: dict[int, int] = {}                 # setup key -> bar it was first seen ENTERED
    final: dict[int, str] = {}                       # setup key -> its final state, once final
    for n in range(1, len(bars) + 1):
        scan = strategy.scan(bars.prefix(n), memo)
        for key, st in scan.items():
            if st.earliest <= key and (key, st.earliest) not in rep.timing:
                rep.timing.append((key, st.earliest))
            if key in final and st.state != final[key]:
                rep.finality.append((key, n, final[key], st.state))
            if st.state in (ENTERED, DEAD):
                final.setdefault(key, st.state)
            if st.state == ENTERED and key not in first_entry:
                first_entry[key] = st.entry_bar
    batch = sorted(e.bar for e in strategy.detect(bars))
    rep.batch_entries = len(batch)
    streamed = {bar: key for key, bar in first_entry.items()}
    for b in batch:
        if b in streamed:
            rep.matched += 1
            continue
        later = [(k, s) for s, k in streamed.items() if s > b]
        if later:
            k, s = min(later, key=lambda x: x[1])
            rep.lagged.append((k, b, s))
        else:
            rep.missed.append(b)
    for s, k in streamed.items():
        if s not in batch:
            rep.phantom.append((k, s))
    return rep


def assert_contract(strategy, bars: Bars) -> ParityReport:
    rep = check(strategy, bars)
    assert rep.ok, rep.summary()
    memo_free = check(strategy, bars, use_memo=False)
    assert memo_free.matched == rep.matched, "a memoized scan must equal a memo-free scan"
    return rep
