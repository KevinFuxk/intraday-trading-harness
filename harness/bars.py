"""
bars.py — the data atom, and the two streaming bar builders the live engine needs
=================================================================================
`Bars` is one symbol, one session, one timeframe. Everything upstream (broker streams,
the disk cache, synthetic data) produces it; everything downstream (strategies, the
engine, the ledger) consumes it.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date as Date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN_MIN = 9 * 60 + 30                     # 09:30 in minutes — the clock anchor for aggregation
CACHE_ROOT = "cache/bars"


@dataclass
class Bars:
    """ONE symbol, ONE session, ONE timeframe, stored as six PARALLEL lists: bar i is
    (ts[i], o[i], h[i], l[i], c[i], v[i]).

    Plain lists, not pandas, on purpose: a strategy scan re-reads these on every closed
    bar of every symbol, and `b.h[i]` must cost nothing. Pandas belongs at the edges
    (analysis, reporting); plain arrays belong in the hot loop."""
    symbol: str
    date: Date
    timeframe: str
    ts: list[datetime] = field(default_factory=list)
    o: list[float] = field(default_factory=list)
    h: list[float] = field(default_factory=list)
    l: list[float] = field(default_factory=list)
    c: list[float] = field(default_factory=list)
    v: list[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.c)

    def append(self, t, o, h, l, c, v) -> None:
        self.ts.append(t); self.o.append(o); self.h.append(h)
        self.l.append(l); self.c.append(c); self.v.append(v)

    def prefix(self, n: int) -> "Bars":
        """The first n bars: exactly what a live process had seen after n bars closed.
        The no-look-ahead tests are built on this."""
        return Bars(self.symbol, self.date, self.timeframe, self.ts[:n], self.o[:n],
                    self.h[:n], self.l[:n], self.c[:n], self.v[:n])


class MinuteAggregator:
    """Incremental 1-min -> k-min bars, clock-aligned from 09:30 (the same buckets a vendor's
    k-minute file uses on a full session). Calls sink(ts, o, h, l, c, v) the moment a bucket
    CLOSES: either its final minute closed, or a bar from a later bucket arrived (a gap in a
    thin name). One data line per symbol serves every timeframe — no extra subscriptions,
    no extra lag."""

    def __init__(self, k: int, sink):
        self.k, self.sink, self.cur = k, sink, None

    def add(self, t, o, h, l, c, v):
        m = (t.hour * 60 + t.minute - OPEN_MIN) // self.k
        if self.cur and (self.cur["date"] != t.date() or self.cur["m"] != m):
            self._flush()                               # previous bucket closed by a gap / new bucket
        if self.cur is None:
            mm = OPEN_MIN + m * self.k
            self.cur = dict(date=t.date(), m=m, ts=t.replace(hour=mm // 60, minute=mm % 60),
                            o=o, h=h, l=l, c=c, v=v)
        else:
            cu = self.cur
            cu["h"] = max(cu["h"], h); cu["l"] = min(cu["l"], l); cu["c"] = c; cu["v"] += v
        if (t.hour * 60 + t.minute - OPEN_MIN) % self.k == self.k - 1:
            self._flush()                               # the bucket's final minute just closed

    def _flush(self):
        if self.cur:
            bu, self.cur = self.cur, None
            self.sink(bu["ts"], bu["o"], bu["h"], bu["l"], bu["c"], bu["v"])


class TickBarBuilder:
    """Ticks -> 1-min bars. Exists because a broker's delayed data tier typically serves
    historical bars only for COMPLETED sessions, while delayed streaming ticks do flow — so
    today's bars are built locally. A minute closes when the first tick of the next minute
    arrives. Volume is the difference of the cumulative day volume."""

    def __init__(self, sink):
        self.sink, self.cur, self.lastvol = sink, None, None

    def on_tick(self, t, price, cumvol):
        m = t.replace(second=0, microsecond=0)
        if self.cur and self.cur["t"] != m:
            b, self.cur = self.cur, None
            self.sink(b["t"], b["o"], b["h"], b["l"], b["c"], b["v"])
        if price is None or price != price or price <= 0:
            return
        if self.cur is None:
            self.cur = dict(t=m, o=price, h=price, l=price, c=price, v=0.0)
        else:
            cu = self.cur
            cu["h"] = max(cu["h"], price); cu["l"] = min(cu["l"], price); cu["c"] = price
        if cumvol is not None and cumvol == cumvol:
            if self.lastvol is not None and cumvol > self.lastvol and self.cur is not None:
                self.cur["v"] += cumvol - self.lastvol
            self.lastvol = cumvol


# ---------------------------------------------------------------- the disk cache
# Schema: cache/bars/<tf>/<SYM>/<YYYY-MM-DD>.json = [{"t": epoch_ms, "o","h","l","c","v"}, ...]
# Every session cached is a session the future backtest owns forever.

def cache_path(sym: str, day: str, tf: str) -> str:
    return f"{CACHE_ROOT}/{tf}/{sym}/{day}.json"


class CachedBar:
    """Duck-types a broker BarData (.date tz-aware, .open/.high/.low/.close/.volume) so a cached
    session flows through exactly the same ingest path as a live one."""
    __slots__ = ("date", "open", "high", "low", "close", "volume")

    def __init__(self, r: dict):
        self.date = datetime.fromtimestamp(r["t"] / 1000, ET)
        self.open, self.high, self.low = r["o"], r["h"], r["l"]
        self.close, self.volume = r["c"], r.get("v", 0)


def cached_day(sym: str, day: str, tf: str):
    """A cached session as a list of CachedBar, or None. One sentinel bar is appended
    because ingest() drops the final (still-forming, when live) bar."""
    p = cache_path(sym, day, tf)
    if not os.path.exists(p):
        return None
    rows = json.load(open(p))
    return [CachedBar(r) for r in rows] + [CachedBar(rows[-1])] if rows else None


def load_bars(sym: str, day: str, tf: str) -> Bars | None:
    """A cached session as a Bars series (regular trading hours only)."""
    from datetime import time as dtime
    p = cache_path(sym, day, tf)
    if not os.path.exists(p):
        return None
    b = Bars(sym, Date.fromisoformat(day), tf)
    for r in json.load(open(p)):
        t = datetime.fromtimestamp(r["t"] / 1000, ET).replace(tzinfo=None)
        if dtime(9, 30) <= t.time() < dtime(16, 0):
            b.append(t, r["o"], r["h"], r["l"], r["c"], r.get("v", 0))
    return b


def save_rows(sym: str, day: str, tf: str, rows: list[dict]) -> str:
    p = cache_path(sym, day, tf)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(rows, open(p, "w"))
    return p
