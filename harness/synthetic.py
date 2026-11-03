"""
synthetic.py — deterministic synthetic sessions, so everything runs without a broker
====================================================================================
A seeded random walk with regime switches (quiet drift, trend bursts, sell-offs). It has
no structure designed to favour any strategy; it exists so the engine, the replay, the
ledger and the tests can be exercised end to end on any machine.
"""
from __future__ import annotations

import random
from datetime import date as Date, datetime, timedelta

from .bars import Bars


def session(symbol: str = "DEMO", day: Date = Date(2025, 3, 3), seed: int = 7,
            start_price: float = 50.0, minutes: int = 390, tf: str = "1min") -> Bars:
    rng = random.Random(f"{symbol}-{day}-{seed}")
    b = Bars(symbol, day, tf)
    t = datetime.combine(day, datetime.min.time()) + timedelta(hours=9, minutes=30)
    px, drift, regime_left = start_price, 0.0, 0
    for _ in range(minutes):
        if regime_left <= 0:                         # a new regime every 15-60 minutes
            drift = rng.choice([0.0, 0.0, 0.0006, 0.0012, -0.0008])
            regime_left = rng.randint(15, 60)
        regime_left -= 1
        vol = px * 0.0015
        o = px
        c = max(0.5, o * (1 + drift) + rng.gauss(0, vol))
        h = max(o, c) + abs(rng.gauss(0, vol * 0.6))
        l = min(o, c) - abs(rng.gauss(0, vol * 0.6))
        v = max(100, int(rng.gauss(20_000, 8_000)))
        b.append(t, round(o, 2), round(h, 2), round(l, 2), round(c, 2), v)
        px = c
        t += timedelta(minutes=1)
    return b


class BarRow:
    """Duck-types a broker BarData so synthetic bars use the engine's real ingest path."""
    __slots__ = ("date", "open", "high", "low", "close", "volume")

    def __init__(self, t, o, h, l, c, v):
        self.date, self.open, self.high, self.low, self.close, self.volume = t, o, h, l, c, v


def as_bar_list(b: Bars) -> list[BarRow]:
    rows = [BarRow(b.ts[i], b.o[i], b.h[i], b.l[i], b.c[i], b.v[i]) for i in range(len(b))]
    return rows + rows[-1:]                          # ingest() skips the final (forming) bar
