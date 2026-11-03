"""
demo.py — the whole engine, offline, in seconds
===============================================
    python demo.py
    python demo.py --symbols AAA,BBB,CCC,DDD --seed 11

Synthetic sessions -> the demonstration strategy -> the REAL engine in replay mode (the
exact code path a live session runs, with shadow fills instead of broker orders) -> the
referee cross-check -> the no-look-ahead contract check. No broker, no network, no data.

The trades it prints are a random walk meeting a textbook signal. They mean nothing; the
point is to watch the lifecycle: pre-arm, skip, fill, stop, target, EOD, referee.
"""
from __future__ import annotations

import argparse
import os

from harness.config import EngineConfig
from harness.engine import Trader
from harness.parity import check
from harness.synthetic import session, as_bar_list
from strategies import load


class OfflineBroker:
    """Shadow mode never places an order; the engine only asks for account values."""
    def accountValues(self): return []
    def positions(self): return []
    def openTrades(self): return []


def main():
    ap = argparse.ArgumentParser(description="Run the engine offline on synthetic sessions.")
    ap.add_argument("--symbols", default="ALFA,BRAVO,CHARLIE")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--base", type=float, default=25_000, help="virtual sizing equity")
    a = ap.parse_args()

    strategy = load("macx")
    cfg = EngineConfig(replay=True, base=a.base)
    bot = Trader(OfflineBroker(), None, None, cfg, strategy,
                 log_path=os.devnull, fills_csv=None, brackets_csv=None)
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    print(f"OFFLINE DEMO — strategy '{strategy.name}' (demonstration only), shadow engine, "
          f"{len(syms)} synthetic session(s)\n")
    for sym in syms:
        print(f"── {sym} " + "─" * 60)
        bars = session(sym, seed=a.seed, start_price=20 + 15 * len(sym))
        bot.ingest(sym, as_bar_list(bars), report=True)
        bot.referee_once(sym, "1min")
        print(f"  ⚖️  {check(strategy, bars).summary()}\n")

    print("── SESSION SUMMARY " + "─" * 50)
    for c in bot.closed:
        print(f"  {c['ts']:%H:%M}  {c['sym']:<8}{c['kind']:<5} exit {c['exit']:>8.2f}   "
              f"{c['r']:+.2f}R   (peak {c.get('mfe', 0):+.2f}R)")
    print(f"  {len(bot.closed)} trade(s), {sum(c['r'] > 0 for c in bot.closed)} win(s), "
          f"{sum(c['r'] for c in bot.closed):+.2f}R — a random walk; the number is not the point.")


if __name__ == "__main__":
    main()
