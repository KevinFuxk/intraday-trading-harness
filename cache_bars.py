"""
cache_bars.py — accumulate a private intraday dataset, one session at a time
============================================================================
    python cache_bars.py                         # the latest archived watchlist, the last session
    python cache_bars.py 2025-03-03              # a NAMED session (pulled by date, never "latest")
    python cache_bars.py NVDA,AMD --tf 15s

For the symbols on the day's watchlist, pull the session's bars from the broker and save
them to cache/bars/<tf>/<SYM>/<YYYY-MM-DD>.json. Replays then read from disk and never touch
the broker again. Idempotent: a (symbol, day) already on disk is never re-pulled.

Why it matters: a curated, news-driven universe cannot be bought as history from a vendor.
Every session cached is a session the future backtest owns.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from datetime import datetime, timedelta

from harness import watchlist
from harness.bars import ET, cache_path, save_rows
from harness.config import PAPER_PORT, PATHS

BAR_SIZE = {"15s": "15 secs", "1min": "1 min", "5min": "5 mins"}


def last_session() -> str:
    """The most recent COMPLETED weekday session (today after 16:00 ET)."""
    now = datetime.now(ET)
    d = now.date() if now.hour >= 16 else now.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def main() -> None:
    ap = argparse.ArgumentParser(description="Cache one session's bars for the day's watchlist.")
    ap.add_argument("args", nargs="*", help="optional YYYY-MM-DD and/or comma-separated symbols")
    ap.add_argument("--tf", default="1min", choices=sorted(BAR_SIZE))
    ap.add_argument("--port", type=int, default=PAPER_PORT)
    ap.add_argument("--client-id", type=int, default=52)
    a = ap.parse_args()
    day = next((x for x in a.args if x[:4].isdigit()), None) or last_session()
    cli = next((x for x in a.args if not x[:4].isdigit()), None)
    archived = sorted(glob.glob(os.path.join(PATHS.watchlists, "*.txt")))
    src = os.path.join(PATHS.watchlists, f"{day}.txt")
    syms, where = watchlist.read(cli, src if os.path.exists(src) else (archived[-1] if archived else "auto"))
    print(f"caching {a.tf} bars for {day}: {len(syms)} symbol(s) from {where}")

    try:
        from ib_async import IB, Stock
    except ImportError:
        sys.exit("✗ ib_async is not installed:  pip install ib_async")
    ib = IB()
    try:
        ib.connect("127.0.0.1", a.port, clientId=a.client_id, timeout=10)
    except Exception as e:
        sys.exit(f"✗ cannot reach IB Gateway on {a.port} ({e})")

    end = datetime.fromisoformat(day).replace(hour=23, minute=59, tzinfo=ET)
    saved = skipped = failed = 0
    for sym in syms:
        if os.path.exists(cache_path(sym, day, a.tf)):
            skipped += 1                             # on disk: no broker request at all
            continue
        c = Stock(sym, "SMART", "USD")
        try:
            ib.qualifyContracts(c)
            if not getattr(c, "conId", 0):
                raise ValueError("unknown contract")
            # Pulled BY DATE. An early version asked for "the latest session" and silently cached
            # the wrong day whenever it ran after the next open.
            bl = ib.reqHistoricalData(c, endDateTime=end, durationStr="1 D", barSizeSetting=BAR_SIZE[a.tf],
                                      whatToShow="TRADES", useRTH=True, formatDate=2, keepUpToDate=False)
        except Exception as e:
            print(f"  {sym}: FAILED ({e})")
            failed += 1
            continue
        rows = [{"t": int(x.date.timestamp() * 1000), "o": float(x.open), "h": float(x.high),
                 "l": float(x.low), "c": float(x.close), "v": float(x.volume)}
                for x in bl if x.date.astimezone(ET).date().isoformat() == day]
        if not rows:
            print(f"  {sym}: no bars for {day}")
            failed += 1
            continue
        print(f"  {sym}: {len(rows)} bars -> {save_rows(sym, day, a.tf, rows)}")
        saved += 1
        ib.sleep(1)                                  # broker pacing
    ib.disconnect()
    print(f"done: {saved} saved, {skipped} already cached, {failed} failed")


if __name__ == "__main__":
    main()
