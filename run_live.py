"""
run_live.py — run a strategy against Interactive Brokers: shadow, armed, or replay
==================================================================================
    python run_live.py                                  # SHADOW: real-time data, narrates, places nothing
    python run_live.py --arm --base 25000               # ARMED: places PAPER orders, sized as a $25k account
    python run_live.py --replay --day 2025-03-03        # REPLAY a completed session through the same code path
    python run_live.py NVDA,AMD --tf 1min,5min          # explicit symbols, two timeframes from one stream

Requires IB Gateway / TWS in PAPER mode with the API enabled. Live ports are refused.

Per symbol: qualify the contract -> screen the universe on the session's open -> seed the
session's history -> attach the stream. The same path serves startup and mid-session
hot-add, so a symbol added at 10:30 behaves exactly like one present at the open.

The main thread does three things every 30 s: hot-add new watchlist symbols, run the
bracket watchdog, and keep the event loop turning. Ctrl-C or ANY uncaught exception
flattens the book before exiting — the process never dies silently holding positions.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, time as dtime, timedelta

from harness import ledger, universe, watchlist
from harness.bars import ET, TickBarBuilder, cached_day
from harness.config import EngineConfig, UniverseRules, LIVE_PORTS, PAPER_PORT, PATHS
from harness.engine import Trader, VERIFY_ATTEMPTS, VERIFY_DELAY_S
from strategies import load as load_strategy

CHATTER = {2104, 2106, 2107, 2108, 2119, 2158, 1102}     # broker farm / data-status notices


def parse_tfs(spec: str, delayed: bool):
    """-> (base_tf, broker bar size, seed duration, minute multiples to aggregate locally)."""
    tfs = [t.strip() for t in spec.split(",")]
    if tfs == ["15s"]:
        if delayed:
            sys.exit("✗ 15-second bars need a real-time subscription.")
        return tfs, "15s", "15 secs", "2 D", []
    if "15s" in tfs:
        sys.exit("✗ 15s runs alone (no aggregation from a 15-second base).")
    if "1min" not in tfs:
        sys.exit("✗ --tf must include 1min: it is the stream every other timeframe is built from.")
    try:
        agg = sorted({int(t[:-3]) for t in tfs if t != "1min"})
        assert all(t.endswith("min") and int(t[:-3]) > 0 for t in tfs)
    except (ValueError, AssertionError):
        sys.exit(f"✗ bad --tf '{spec}' — use e.g. 1min,2min,5min or 15s")
    # 5 D, not 1 D: the window must always contain at least one session (holiday weekends).
    return tfs, "1min", "1 min", "5 D", agg


def main():
    d = EngineConfig()
    ap = argparse.ArgumentParser(description="Run a strategy on IBKR paper: shadow / armed / replay.")
    ap.add_argument("symbols", nargs="?", default=None, help="comma-separated tickers (else --watchlist)")
    ap.add_argument("--strategy", default="macx")
    ap.add_argument("--watchlist", default="auto", help="a file, or 'auto' = the newest export in --watchlist-dir")
    ap.add_argument("--watchlist-dir", default=watchlist.SEARCH_DIR)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=PAPER_PORT)
    ap.add_argument("--client-id", type=int, default=8, help="one per running process")
    ap.add_argument("--arm", action="store_true", help="actually place PAPER orders (default: shadow)")
    ap.add_argument("--risk", type=float, default=d.risk)
    ap.add_argument("--tp", type=float, default=d.tp, help="take-profit in R")
    ap.add_argument("--minstop", type=float, default=d.minstop, help="minimum stop, %% of price (0 = off)")
    ap.add_argument("--max-positions", type=int, default=d.max_positions)
    ap.add_argument("--base", type=float, default=d.base, help="virtual sizing equity (0 = real NetLiq)")
    ap.add_argument("--eod", default=f"{d.eod:%H:%M}", help="flatten time, HH:MM ET")
    ap.add_argument("--min-price", type=float, default=UniverseRules().min_price)
    ap.add_argument("--tf", default="1min")
    ap.add_argument("--delayed", action="store_true", help="free delayed data (plumbing only)")
    ap.add_argument("--poll", action="store_true", help="re-request history on a timer instead of streaming")
    ap.add_argument("--replay", action="store_true", help="walk a completed session through the live code path")
    ap.add_argument("--day", default=None, help="replay: the session date YYYY-MM-DD (read from cache when present)")
    ap.add_argument("--i-understand-live", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()

    if a.port in LIVE_PORTS and not a.i_understand_live:
        sys.exit(f"✗ port {a.port} is a LIVE trading port — this harness is paper-only. Refusing.")
    if a.replay and a.arm:
        sys.exit("✗ --replay with --arm makes no sense: replay is shadow-only.")
    hh, mm = (int(x) for x in a.eod.split(":"))
    cfg = EngineConfig(arm=a.arm, replay=a.replay, risk=a.risk, tp=a.tp, minstop=a.minstop,
                       max_positions=a.max_positions, base=a.base, eod=dtime(hh, mm))
    rules = UniverseRules(min_price=a.min_price)
    strategy = load_strategy(a.strategy)
    syms, src = watchlist.read(a.symbols, a.watchlist, a.watchlist_dir)
    tfs, base_tf, bar_size, seed_dur, agg_ks = parse_tfs(a.tf, a.delayed)

    try:
        from ib_async import IB, Stock, Order, MarketOrder
    except ImportError:
        sys.exit("✗ ib_async is not installed:  pip install ib_async")
    ib = IB()
    try:
        ib.connect(a.host, a.port, clientId=a.client_id, timeout=10)
    except Exception as e:
        sys.exit(f"✗ cannot reach IB Gateway/TWS at {a.host}:{a.port} ({e})")
    ib.reqMarketDataType(3 if a.delayed else 1)
    ib.sleep(1)

    bot = Trader(ib, Order, MarketOrder, cfg, strategy, agg_ks, base_tf=base_tf)

    def on_error(reqId, code, msg, contract=None):
        """Rejections and disconnects belong in the day's record, not just on a terminal."""
        if code in CHATTER:
            return
        sym = f" {contract.symbol}" if contract is not None and getattr(contract, "symbol", "") else ""
        bot._say(f"  ⚠️ broker error {code}{sym} (req {reqId}): {msg}")
    ib.errorEvent += on_error

    print(f"{strategy.name.upper()} — IBKR paper — {'🔴 ARMED' if a.arm else '🟢 SHADOW'}"
          f"{'  (REPLAY)' if a.replay else ''}")
    print(f"  sizing equity ${bot.equity():,.0f}{'  (virtual --base)' if a.base > 0 else ''}")
    print(f"  tf {','.join(tfs)} · risk {a.risk * 100:.2f}% · tp {a.tp:g}R · min stop {a.minstop:g}% · "
          f"cap {a.max_positions} · EOD {a.eod}")
    print(f"  watchlist ({len(syms)}) from {src}\n  watching {', '.join(syms)}")
    if not a.replay:
        watchlist.archive(watchlist.resolve(a.watchlist, a.watchlist_dir))
    print()

    def on_update(bars, has_new_bar):
        if has_new_bar:
            bot.ingest(bars.contract.symbol, bars, report=True)

    stream = not a.poll and not a.replay
    skipped: list[str] = []
    universe_rows: list[dict] = []
    seeds: dict = {}                                  # symbol -> its live bar subscription
    details: dict = {}                                # symbol -> broker contract details
    exp = watchlist.resolve(a.watchlist, a.watchlist_dir) if not a.symbols else None
    sources = watchlist.parse_sources(exp) if exp else {}

    def drop(s: str, why: str, say=print, sub=None) -> None:
        if s not in skipped:
            skipped.append(s)
        bot.blocked.add(s)
        bot.unscreened.discard(s)
        bot.contracts.pop(s, None)
        for k in [k for k in bot.store if k[0] == s]:
            bot.store.pop(k, None)
        sub = sub if sub is not None else seeds.pop(s, None)
        if sub is not None:
            try:
                sub.updateEvent -= on_update
                ib.cancelHistoricalData(sub)
            except Exception:
                pass
        say(f"  🚫 {s}: DROPPED — {why}")

    def note(s: str, price, why: str) -> dict:
        r = universe.row(f"{datetime.now(ET):%Y-%m-%d}", s, sources.get(s, []), price, why)
        universe_rows[:] = [x for x in universe_rows if x["symbol"] != s] + [r]
        return r

    def universe_hook(sym: str, open_px: float) -> None:
        """A symbol admitted before the open is judged here, on the session's first bar."""
        why = universe.verdict(rules, sym, open_px, details.get(sym))
        universe.record([note(sym, open_px, why)])
        if why:
            drop(sym, why, bot._say)
        else:
            bot._say(f"  ✅ {sym}: opened ${open_px:.2f} — passes the universe screen")
    bot.universe_hook = universe_hook

    def setup_symbol(s: str, say=print) -> bool:
        c = Stock(s, "SMART", "USD")
        try:
            ib.qualifyContracts(c)
            if not getattr(c, "conId", 0):          # qualify returns truthy even on rejection
                raise ValueError("unknown contract — not a US stock at the broker")
        except Exception as ex:
            skipped.append(s)
            say(f"  {s}: SKIPPED — {ex}")
            return False
        bot.contracts[s] = c
        cached = cached_day(s, a.day, base_tf) if (a.replay and a.day) else None
        if cached is not None:
            bl = cached                                # replay from disk: no broker pull
        elif a.replay and a.day:                       # a named day: pull THAT day, never "latest"
            end = datetime.fromisoformat(a.day).replace(hour=23, minute=59, tzinfo=ET)
            bl = ib.reqHistoricalData(c, endDateTime=end, durationStr="1 D", barSizeSetting=bar_size,
                                      whatToShow="TRADES", useRTH=True, formatDate=2, keepUpToDate=False)
        else:
            bl = ib.reqHistoricalData(c, endDateTime="", durationStr=seed_dur, barSizeSetting=bar_size,
                                      whatToShow="TRADES", useRTH=True, formatDate=2, keepUpToDate=stream)
        sub = bl if (stream and cached is None) else None
        if not len(bl):
            drop(s, "no bars served (halted or not trading)", say, sub)
            return False

        def _d(x):
            return x.date.astimezone(ET).date() if hasattr(x.date, "astimezone") else x.date.date()
        feed, ref_day = bl, datetime.now(ET).date()
        if a.replay:                                   # replay exactly one session
            ref_day = max(_d(x) for x in bl)
            feed = [x for x in bl if _d(x) == ref_day]
        # THE UNIVERSE SCREEN — on the session's OPEN, before a single bar is walked (I-8).
        first = next((x for x in feed if _d(x) == ref_day), None)
        price = float(first.open) if first is not None else None
        try:
            cds = ib.reqContractDetails(c)
            details[s] = cds[0] if cds else None
        except Exception:
            details[s] = None
        why = universe.verdict(rules, s, price, details[s])
        if not a.replay:
            note(s, price, why)
        if why:
            drop(s, why, say, sub)
            return False
        if price is None and not a.replay:
            bot.unscreened.add(s)                      # pre-open: judged on the first bar
        was_live = bot._report
        bot.ingest(s, feed, report=a.replay)
        bot._report = was_live or a.replay             # a mid-session seed must not silence the board
        if a.replay:
            for tf_ in tfs:
                bot.referee_once(s, tf_)
        if sub is not None:
            bl.updateEvent += on_update
            seeds[s] = bl
        seeded = " ".join(f"{len(bot.store.get((s, t), []))} {t}" for t in tfs)
        say(f"  {s}: seeded {seeded} bars" + (" (from cache)" if cached is not None else "")
            + (" — price rule runs on the open" if price is None and not a.replay else ""))
        return True

    for s in syms:
        setup_symbol(s)
    if universe_rows:
        universe.record(universe_rows)
        print(f"  📚 universe verdicts -> {PATHS.universe} "
              f"({sum(r['qualified'] == 'yes' for r in universe_rows)} tradable, "
              f"{sum(r['qualified'] == 'no' for r in universe_rows)} dropped with reasons)")
    syms = [s for s in syms if s not in skipped]
    if not syms:
        sys.exit("✗ no tradable symbols left")

    if a.replay:
        latest = max((B.ts[-1] for (s, t), B in bot.store.items() if t == base_tf and len(B)), default=None)
        day = f"{latest:%Y-%m-%d}" if latest else "unknown"
        print(f"\n▶️ REPLAY complete — {day}. Every line above is what live would have printed.")
        for c in bot.closed:
            print(f"  {c['ts']:%H:%M}  {c['sym']:<7}{c['tf']:<6}{c['kind']:<6}{c['exit']:>10.2f}{c['r']:>+8.2f}R")
        print(f"  {len(bot.closed)} trade(s), {sum(c['r'] > 0 for c in bot.closed)} win(s), "
              f"{sum(c['r'] for c in bot.closed):+.2f}R")
        variant = f"{strategy.name}:replay:minstop={a.minstop:g}"
        ledger.write_variant(day, variant, ledger.from_closed(bot.closed))
        print(f"  📒 {len(bot.closed)} row(s) -> {PATHS.ledger} ({day}, {variant})")
        ib.disconnect()
        return

    today = datetime.now(ET).date()
    latest = max((B.ts[-1] for (s, t), B in bot.store.items() if t == base_tf and len(B)), default=None)
    tickmode = a.delayed and (latest is None or latest.date() < today)
    if tickmode:
        # The delayed tier's history serves completed sessions only; build today from ticks, and
        # DETACH the historical streams — on reconnects they re-deliver old sessions, whose EOD
        # bar would fire a spurious mid-day flatten.
        for s_, sub in seeds.items():
            try:
                sub.updateEvent -= on_update
                ib.cancelHistoricalData(sub)
            except Exception:
                pass
        lag = timedelta(minutes=15)
        bot._report = True
        builders = {s: TickBarBuilder((lambda s_: lambda *bar: bot.feed_bar(s_, *bar))(s)) for s in syms}
        for s in syms:
            ib.reqMktData(bot.contracts[s], "", False, False)

        def on_ticks(tickers):
            now = datetime.now(ET).replace(tzinfo=None) - lag
            for tk in tickers:
                b = builders.get(tk.contract.symbol)
                if b and tk.last is not None and tk.last == tk.last and tk.last > 0:
                    b.on_tick(now, tk.last, tk.volume)
        ib.pendingTickersEvent += on_ticks

    def hot_add():
        """Additive only, today's export only, session hours only, never fatal: an exception
        here on the main thread would skip the kill-switch flatten."""
        if a.symbols or tickmode:
            return
        now = datetime.now(ET)
        if not (dtime(9, 30) <= now.time() < cfg.eod):
            return
        try:
            path = watchlist.resolve(a.watchlist, a.watchlist_dir)
            new = watchlist.additions(set(bot.contracts) | set(skipped), path, now.date())
            if not new:
                return
            bot._say(f"  🧩 {now:%H:%M} watchlist grew — adding {', '.join(new)}")
            sources.update(watchlist.parse_sources(path))
            for s_ in new:
                if setup_symbol(s_, say=bot._say):
                    syms.append(s_)
            fresh = [r for r in universe_rows if r["symbol"] in new]
            if fresh:
                universe.record(fresh)
            watchlist.archive(path)
        except Exception as ex:
            bot._say(f"  ⚠️ hot-add failed ({type(ex).__name__}: {ex}) — still trading the current list")

    def flatten_and_confirm(reason: str):
        bot.flatten(reason)
        for attempt in range(1, VERIFY_ATTEMPTS + 1):  # no running event loop here: verify by hand
            ib.sleep(VERIFY_DELAY_S)
            if bot.verify_flatten(attempt):
                break

    print(f"running{' (polling)' if a.poll else ''}…  (Ctrl-C flattens and exits)\n")
    try:
        while True:
            ib.sleep(max(30, len(syms) * 12) if a.poll else 30)   # pacing: <= 60 history requests / 10 min
            hot_add()
            bot.guard_brackets()
            if a.poll:
                for s in list(syms):
                    pl = ib.reqHistoricalData(bot.contracts[s], endDateTime="", durationStr="1 D",
                                              barSizeSetting=bar_size, whatToShow="TRADES",
                                              useRTH=True, formatDate=2, keepUpToDate=False)
                    bot.ingest(s, pl, report=True)
    except KeyboardInterrupt:
        print("\n⛔ kill-switch — flattening…")
        flatten_and_confirm("kill-switch")
    except Exception as ex:                          # never exit silently with positions open
        print(f"\n💥 fatal: {type(ex).__name__}: {ex} — flattening…")
        try:
            flatten_and_confirm("fatal error")
        except Exception as ex2:
            print(f"   flatten impossible ({ex2}) — CHECK THE ACCOUNT MANUALLY")
        raise
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
