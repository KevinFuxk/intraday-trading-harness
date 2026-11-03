"""Test doubles: a broker that never fills on its own, and a strategy that says exactly what
the test tells it to. Together they let every order-state path be driven deterministically."""
from __future__ import annotations

import itertools
import os
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS

from harness.bars import Bars
from harness.config import EngineConfig
from harness.engine import Trader
from harness.strategy import Setup, FORMING, ENTERED, DEAD

DAY = date(2025, 3, 3)


def flat_bars(n: int, px: float = 100.0, sym: str = "TEST") -> Bars:
    b = Bars(sym, DAY, "1min")
    t0 = datetime.combine(DAY, datetime.min.time()) + timedelta(hours=9, minutes=30)
    for i in range(n):
        b.append(t0 + timedelta(minutes=i), px, px + 0.05, px - 0.05, px, 1000)
    return b


def forming(key, trigger, stop, earliest, label=""):
    return {key: Setup(key, FORMING, trigger, stop, earliest, label=label)}


def entered(key, trigger, stop, earliest, entry_bar):
    return {key: Setup(key, ENTERED, trigger, stop, earliest, entry_bar=entry_bar)}


def dead(key, trigger, stop, earliest):
    return {key: Setup(key, DEAD, trigger, stop, earliest)}


class ScriptedStrategy:
    """scan() returns script[len(bars)] — the setups a test dictates for each prefix length."""

    def __init__(self, script: dict, name: str = "test", entries=()):
        self.script, self.name, self.entries = script, name, list(entries)

    def scan(self, b, memo=None):
        return self.script.get(len(b), {})

    def detect(self, b):
        return self.entries


class Ev:
    """ib_async-style event: `trade.fillEvent += handler`."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, f):
        self.handlers.append(f)
        return self

    def fire(self, *args):
        for f in self.handlers:
            f(*args)


class FakeTrade:
    def __init__(self, contract, order):
        self.contract, self.order = contract, order
        self.orderStatus = NS(status="Submitted", filled=0, avgFillPrice=0)
        self.fillEvent, self.log = Ev(), []


class FakeBroker:
    """Records every order. Fills nothing by itself: a test decides what the broker filled,
    by setting orderStatus.filled and/or firing fillEvent."""

    def __init__(self):
        self.trades, self.cancelled, self.pos = [], [], {}
        self.client = NS(getReqId=itertools.count(100).__next__)

    def accountValues(self):
        return []

    def positions(self):
        return [NS(contract=NS(symbol=s, exchange="NASDAQ"), position=q) for s, q in self.pos.items() if q]

    def openTrades(self):
        return [t for t in self.trades if t.orderStatus.status in ("Submitted", "PreSubmitted")]

    def placeOrder(self, contract, order):
        t = FakeTrade(contract, order)
        self.trades.append(t)
        return t

    def cancelOrder(self, order):
        self.cancelled.append(order.orderRef)
        for t in self.trades:
            if t.order is order:
                t.orderStatus.status = "Cancelled"

    def qualifyContracts(self, c):
        return [c]


def order(**kw):
    return NS(**kw)


def market_order(action, qty):
    return NS(action=action, orderType="MKT", totalQuantity=qty, auxPrice=0, lmtPrice=0, orderRef="")


def make_trader(strategy, arm=False, replay=False, broker=None, symbols=("TEST",), **cfg):
    broker = broker or FakeBroker()
    cfg.setdefault("base", 10_000)
    cfg.setdefault("minstop", 0.0)
    t = Trader(broker, order, market_order, EngineConfig(arm=arm, replay=replay, **cfg), strategy,
               log_path=os.devnull, fills_csv=None, brackets_csv=None)
    said: list[str] = []
    t._say = said.append
    t._report = True
    t.contracts = {s: NS(symbol=s, exchange="SMART") for s in symbols}
    return t, broker, said


def fire_fill(trade, shares: int, price: float):
    trade.fillEvent.fire(trade, NS(execution=NS(price=price, shares=shares), time="2025-03-03 10:00:00"))
