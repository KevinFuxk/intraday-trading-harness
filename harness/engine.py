"""
engine.py — the order engine: pre-armed brackets, broker-truth state, own-book accounting
=========================================================================================
One `Trader` runs one strategy on one account. It turns a stream of CLOSED bars into a
lifecycle of native broker brackets, and it treats the broker — never the bars — as the
truth about what it holds.

    PRE-ARM   a FORMING setup whose next bar may legally fill -> place a native bracket:
              BUY-STOP parent at the trigger, OCA children: LMT take-profit at +tp R,
              STP stop-loss at the setup's stop. The order rests BEFORE the breakout, so a
              fill happens at the first touch — the fill a backtest assumes.
    UPDATE    the setup's stop moves down -> cancel and re-place the UNFILLED bracket under
              a NEW ref (OCA children cannot be revised in place; I-4)
    CANCEL    the setup dies -> cancel the bracket (the children die with the parent)
    FILL      "entered" means the broker FILLED the parent — not that a bar crossed the
              trigger (I-5). A crossing bar with no fill gets one bar of grace, then the
              bracket is cancelled and recorded as a MISSED FILL.
    BOOK      every fill is booked against its order ref: ENTRY adds shares, TP / SL / EOD
              subtract them, and a trade closes only when its ref is back to zero (I-3).
    WATCHDOG  every 30 s: every share on MY book has a working stop, no exit order is larger
              than the book, a flat book has no exit orders left (I-3, I-6)
    FLATTEN   at the configured EOD or on Ctrl-C: cancel MY orders, close MY book, then read
              the result back from the broker before believing it (I-1, I-2, I-9)

"MY" is load-bearing. Several strategies can share one account; each Trader namespaces its
order refs with its strategy name and touches only those. The account view (positions,
open orders) is read for alarms, never acted on (I-6).

SHADOW mode (the default) narrates every step and places nothing; REPLAY walks a completed
session through the identical code path — the only difference from live is where the bars
come from.

Incident references (I-n) point to docs/INCIDENTS.md.
"""
from __future__ import annotations

import os
import sys
import time as _time
from collections import defaultdict
from datetime import datetime, time as dtime

from .bars import Bars, MinuteAggregator, ET
from .config import EngineConfig, PATHS
from .strategy import FORMING, ENTERED, DEAD

DONE_STATES = ("Filled", "Cancelled", "ApiCancelled", "Inactive")
WORKING_STATES = ("PreSubmitted", "Submitted", "PendingSubmit")
RTH_OPEN, RTH_CLOSE = dtime(9, 30), dtime(16, 0)
FLATTEN_WINDOW_S = 15            # a second flatten inside this window places nothing (I-2)
VERIFY_DELAY_S = 3
VERIFY_ATTEMPTS = 3              # a working close is re-checked before it is called a failure (I-9)
HEARTBEAT_S = 900
FILLS_HEADER = "time,symbol,ref,leg,action,shares,level,fill,slip_cents"
BRACKETS_HEADER = "time,ref,symbol,tf,event,trigger,stop,target,qty"


def _append_csv(path: str | None, header: str, line: str) -> None:
    if not path:
        return
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        new = not os.path.exists(path)
        with open(path, "a") as f:
            if new:
                f.write(header + "\n")
            f.write(line + "\n")
    except OSError:
        pass


class Trader:
    """One strategy, one account. See the module docstring for the lifecycle."""

    def __init__(self, ib, Order, MarketOrder, cfg: EngineConfig, strategy, agg_ks=(),
                 base_tf: str = "1min", log_path: str | None = None,
                 fills_csv: str | None = PATHS.fills, brackets_csv: str | None = PATHS.brackets):
        self.ib, self.Order, self.MarketOrder, self.a = ib, Order, MarketOrder, cfg
        self.strategy = strategy
        self.base_tf = base_tf                       # the stream's native bar size
        self.agg_ks = list(agg_ks)                   # minute multiples aggregated locally, e.g. [2, 5]
        self.aggs: dict[tuple, MinuteAggregator] = {}
        self.store: dict[tuple, Bars] = {}           # (sym, tf) -> today's closed bars
        self.fired: dict[tuple, set] = defaultdict(set)
        self.pending: dict[str, dict] = {}           # sym -> the ONE resting setup (any timeframe)
        self.entered_at: dict[tuple, set] = defaultdict(set)   # (sym, tf) -> EVERY bar our resting order filled
        self.seen: set[str] = set()                  # once-only log guard
        self.contracts: dict[str, object] = {}
        self.eod_done = False
        self._report = False                         # False while seeding history, True when live / replaying
        self.eq0 = None
        self.live_started = False
        self.last_beat = None
        self._live_bars = 0
        self.cur_day = None
        self.paper: list = []                        # SHADOW: simulated open positions
        self.closed: list = []                       # today's completed trades
        self.open_real: dict = {}                    # ARMED: order ref -> position, built ONLY from fills
        self.brackets: dict = {}                     # order ref -> the levels it was placed at
        self._last_t = None
        self._drawn = 0.0
        self._last_flatten = 0.0
        self._memo: dict[tuple, dict] = {}           # (sym, tf) -> strategy scan memo for the session
        self._inflight: dict = {}                    # ref -> its working close (never double-close; I-2)
        self._parent_trades: dict = {}               # parent orderId -> Trade (is a child live yet?)
        self._superseded: dict = {}                  # old ref -> new ref after a re-place (fill-race guard)
        self._closes: list = []
        self.blocked: set = set()                    # symbols the universe screen dropped
        self.unscreened: set = set()                 # admitted pre-open; judged on their first bar (I-8)
        self.universe_hook = None                    # runner callback: (sym, open_price) -> screen now
        self._acct_noted: set = set()
        self.log_path = log_path or os.path.join(
            PATHS.logs, f"{strategy.name}_{datetime.now(ET):%Y-%m-%d}.log")
        self.fills_csv, self.brackets_csv = fills_csv, brackets_csv

    # ------------------------------------------------------------------ account
    def equity(self) -> float:
        """Sizing equity. With cfg.base = N: a VIRTUAL account of N that compounds with this
        session's P&L, so a large paper account can be sized like a small real one."""
        nl = 0.0
        for v in self.ib.accountValues():
            if v.tag == "NetLiquidation" and v.currency == "USD":
                nl = float(v.value)
                break
        if self.a.base > 0:
            if self.eq0 is None and nl > 0:
                self.eq0 = nl
            return self.a.base + (nl - (self.eq0 if self.eq0 is not None else nl))
        return nl

    def occupied_syms(self) -> set:
        """Symbols THIS strategy is committed to: a resting bracket or an open position.
        Own book only — never the account's positions, which also hold other strategies'
        shares. Reading those made one strategy refuse its own setups and burn position
        slots on shares that were not its own (I-6)."""
        own = set(self.pending)
        own |= {p["sym"] for p in self.paper}
        own |= {v["sym"] for v in self.open_real.values()}
        return own

    # ------------------------------------------------------------------ output
    def _say(self, line: str) -> None:
        """Lifecycle narration. Replay prints it (the audit trail). Live appends it to the
        session log and redraws the status board, so the terminal stays a dashboard."""
        if self.a.replay:
            print(line)
            return
        try:
            d = os.path.dirname(self.log_path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.log_path, "a") as f:
                f.write(line.strip() + "\n")
        except OSError:
            pass
        self._draw(force=True)

    def _draw(self, force: bool = False) -> None:
        """Full-screen status board: resting brackets, open positions next to their exits,
        today's closed trades."""
        if self.a.replay or not self._report:
            return
        if not force and _time.time() - self._drawn < 2:
            return
        self._drawn = _time.time()
        dayr = sum(c["r"] for c in self.closed)
        hdr_t = f"{self._last_t:%a %m-%d %H:%M}" if self._last_t else "waiting for bars"
        L = [f"{self.strategy.name.upper()} — {'🔴 ARMED' if self.a.arm else '🟢 SHADOW'}  |  {hdr_t}  |  "
             f"{self._live_bars} live bars  |  sizing equity ${self.equity():,.0f}  |  day {dayr:+.2f}R",
             "═" * 100, "", "RESTING BRACKETS — buy-stop waiting for the trigger"]
        if self.pending:
            L.append(f"  {'sym':<7}{'tf':<6}{'since':<8}{'trigger':>10}{'stop':>10}{'take-profit':>13}{'qty':>8}")
            for sym, p in self.pending.items():
                since = f"{p['ts']:%H:%M}" if p.get("ts") else "—"
                L.append(f"  {sym:<7}{p['tf']:<6}{since:<8}{p['trigger']:>10.2f}{p['stop']:>10.2f}"
                         f"{p['target']:>13.2f}{p['qty']:>8}")
        else:
            L.append("  none — scanning")
        L += ["", "OPEN POSITIONS" + ("" if self.a.arm else " (simulated)")]
        rows = [(p["sym"], p["tf"], p["entry"], p["stop"], p["target"], p["trigger"])
                for p in (self.open_real.values() if self.a.arm else self.paper)]
        if rows:
            L.append(f"  {'sym':<7}{'tf':<6}{'entry':>10}{'stop':>10}{'take-profit':>13}{'last':>10}{'uP&L':>9}")
            for sym, tf, entry, stop, target, trig in rows:
                B = self.store.get((sym, self.base_tf))
                last = B.c[-1] if B and len(B) else entry
                risk = trig - stop
                ur = (last - entry) / risk if risk > 0 else 0.0
                L.append(f"  {sym:<7}{tf:<6}{entry:>10.2f}{stop:>10.2f}{target:>13.2f}{last:>10.2f}{ur:>+8.2f}R")
        else:
            L.append("  none")
        L += ["", "CLOSED TODAY"]
        L += ([f"  {c['ts']:%H:%M}  {c['sym']:<7}{c['tf']:<6}{c['kind']:<5}@ {c['exit']:>9.2f}   {c['r']:+.2f}R"
               for c in self.closed[-8:]] or ["  none yet"])
        L += ["", f"log -> {self.log_path} · fills -> {self.fills_csv} · Ctrl-C = flatten + quit"]
        sys.stdout.write("\x1b[2J\x1b[H" + "\n".join(L) + "\n")
        sys.stdout.flush()

    # ------------------------------------------------------------------ order plumbing
    def place_bracket(self, sym: str, pend: dict, event: str = "PLACED") -> dict:
        """Native bracket: STP parent + OCA (LMT take-profit, STP stop-loss) children, sent
        as one unit (only the last leg transmits). ocaType=2: a PARTIAL fill of one exit
        REDUCES the other proportionally. ocaType=1 cancels the sibling outright on any
        fill, partial included — a small take-profit fill once cancelled the stop for the
        whole position (I-3)."""
        c = self.contracts[sym]
        pid = self.ib.client.getReqId()
        ref = pend["ref"]
        parent = self.Order(orderId=pid, action="BUY", orderType="STP", totalQuantity=pend["qty"],
                            auxPrice=pend["trigger"], tif="DAY", transmit=False, orderRef=ref)
        tp = self.Order(orderId=self.ib.client.getReqId(), action="SELL", orderType="LMT",
                        totalQuantity=pend["qty"], lmtPrice=pend["target"], tif="DAY", parentId=pid,
                        transmit=False, orderRef=ref, ocaGroup=ref, ocaType=2)
        sl = self.Order(orderId=self.ib.client.getReqId(), action="SELL", orderType="STP",
                        totalQuantity=pend["qty"], auxPrice=pend["stop"], tif="DAY", parentId=pid,
                        transmit=True, orderRef=ref, ocaGroup=ref, ocaType=2)
        trades = {}
        for name, o in (("ENTRY", parent), ("TP", tp), ("SL", sl)):
            tr = self.ib.placeOrder(c, o)
            tr.fillEvent += self._fill_logger(name)
            trades[name] = tr
        self._parent_trades[pid] = trades["ENTRY"]   # children protect nothing until this fills
        self.brackets[ref] = dict(pend)              # SNAPSHOT: each ref keeps the levels it was sent with
        _append_csv(self.brackets_csv, BRACKETS_HEADER,
                    f"{datetime.now(ET):%Y-%m-%d %H:%M:%S},{ref},{sym},{pend['tf']},{event},"
                    f"{pend['trigger']:.2f},{pend['stop']:.2f},{pend['target']:.2f},{pend['qty']}")
        return trades

    def _cancel_order(self, order) -> bool:
        try:
            self.ib.cancelOrder(order)
            return True
        except Exception as ex:
            self._say(f"  ⚠️ cancel failed for {getattr(order, 'orderRef', '?')}: {ex}")
            return False

    def _cancel_parent(self, pend: dict, why: str) -> bool:
        """Cancel a resting bracket's parent; unfilled children die with it."""
        ok = self._cancel_order(pend["trades"]["ENTRY"].order)
        if not ok:
            self._say(f"  ⚠️ {pend['ref']} could not be cancelled ({why}) — CHECK THE BROKER")
        return ok

    def _entry_filled(self, pend: dict) -> tuple[int, int]:
        """(shares the broker has filled on the parent, shares ordered) — the only 'entered'
        that counts in armed mode (I-5)."""
        tr = pend["trades"]["ENTRY"]
        return int(getattr(tr.orderStatus, "filled", 0) or 0), int(pend["qty"])

    def _fill_logger(self, kind: str):
        """OWN-BOOK accounting from the broker's fills — the only truth about what is held.
        Every ref carries a share count: ENTRY adds, TP / SL / EOD subtract, and the trade
        is booked closed only when the count reaches ZERO. A partial exit leaves the rest on
        the book, where the watchdog keeps it stopped (I-3). Every fill is also written to
        the fills CSV with its slippage against the level it was meant to fill at."""
        def on_fill(trade, fill):
            o = trade.order
            sym = trade.contract.symbol
            px = float(fill.execution.price)
            sh = int(fill.execution.shares)
            ref = o.orderRef
            if o.orderType == "MKT":                 # EOD / kill-switch close: cost vs the last price seen
                B = self.store.get((sym, self.base_tf))
                level = B.c[-1] if B and len(B) else px
            else:
                level = o.lmtPrice if o.orderType == "LMT" else o.auxPrice
            slip = (px - level) if o.action == "BUY" else (level - px)    # + = worse than intended
            self._say(f"  💰 FILL {kind:5} {sym} {sh}@${px:.2f} (level ${level:.2f}, slip {slip * 100:+.1f}¢)")
            br = self.brackets.get(ref, {})
            now = self._last_t or datetime.now(ET).replace(tzinfo=None)
            if kind == "ENTRY":
                pos = self.open_real.get(ref)
                if pos is None:
                    pos = self.open_real[ref] = dict(sym=sym, tf=br.get("tf", "?"), entry=px, qty=0,
                                                     cost=0.0, out=0.0, out_qty=0,
                                                     stop=br.get("stop", level), target=br.get("target", 0.0),
                                                     trigger=br.get("trigger", level), ts=now)
                pos["qty"] += sh
                pos["cost"] += sh * px
                pos["entry"] = pos["cost"] / pos["qty"]
                nxt = self._superseded.get(ref)
                if nxt:
                    # FILL RACE: this bracket was being cancelled and re-placed, but the fill
                    # beat the cancel. Its OCA exits are live; the successor must never fill
                    # too, or the position doubles (I-4).
                    pend = self.pending.get(sym)
                    if pend and pend["ref"] == nxt and pend["trades"]:
                        self._cancel_parent(pend, "successor of a bracket that filled during its re-place")
                        self.pending.pop(sym, None)
                    self._say(f"  🚨 {sym}: {ref} filled while being re-placed — successor {nxt} cancelled, "
                              f"the original exits stand")
            elif kind in ("TP", "SL", "EOD"):
                pos = self.open_real.get(ref)
                if pos:
                    pos["qty"] -= sh
                    pos["out"] += sh * px
                    pos["out_qty"] += sh
                    if pos["qty"] <= 0:
                        self.open_real.pop(ref, None)
                        exit_px = pos["out"] / pos["out_qty"] if pos["out_qty"] else px
                        risk = pos["trigger"] - pos["stop"]
                        r = (exit_px - pos["entry"]) / risk if risk > 0 else 0.0
                        self.closed.append(dict(ts=now, sym=pos["sym"], tf=pos["tf"], kind=kind,
                                                exit=exit_px, r=r, entry=pos["entry"],
                                                trigger=pos["trigger"], stop=pos["stop"], entry_ts=pos["ts"]))
                    else:
                        self._say(f"  ↕️ {sym}: {sh} sh out on {kind}, {pos['qty']} sh still on the book "
                                  f"(stop ${pos['stop']:.2f} — the watchdog keeps it covered)")
            _append_csv(self.fills_csv, FILLS_HEADER,
                        f"{fill.time},{sym},{ref},{kind},{o.action},{sh},{level:.2f},{px:.2f},{slip * 100:.1f}")
        return on_fill

    # ------------------------------------------------------------------ setup lifecycle
    def arm_pending(self, sym: str, tf: str, key: int, st, B: Bars) -> None:
        if sym in self.blocked:
            return
        ref = f"{self.strategy.name}-{sym}-{tf}-{B.date}-k{key}"
        trigger, stop = round(st.trigger, 2), round(st.stop, 2)
        risk = trigger - stop
        if risk <= 0:
            return
        stop_pct = risk / trigger * 100
        if self.a.minstop > 0 and stop_pct < self.a.minstop:
            # A stop thinner than the execution noise turns a planned -1R into a multiple of
            # it before the market does anything. The floor is a hard gate, not a preference.
            if ref not in self.seen:
                self.seen.add(ref)
                self._say(f"  SKIP {sym} {tf} setup@{key} — stop {stop_pct:.2f}% of price < "
                          f"{self.a.minstop:g}% floor")
            return
        occ = self.occupied_syms()
        if sym in occ or len(occ) >= self.a.max_positions:
            if ref not in self.seen:
                self.seen.add(ref)
                self._say(f"  SKIP {sym} {tf} setup@{key} — symbol occupied or at cap ({self.a.max_positions})")
            return
        eq = self.equity()
        qty = int((self.a.risk * eq) / risk) if eq > 0 else 0
        if qty < 1:
            if ref not in self.seen:
                self.seen.add(ref)
                self._say(f"  SKIP {sym} {tf} — size < 1 share (equity ${eq:,.0f}, risk/sh ${risk:.2f})")
            return
        target = round(trigger + self.a.tp * risk, 2)
        pend = dict(key=key, tf=tf, ref=ref, ref0=ref, gen=1, grace=0, trigger=trigger, stop=stop,
                    qty=qty, target=target, trades=None, ts=B.ts[-1])
        tag = f"  [{st.label}]" if st.label else ""
        line = (f"{sym} {tf} buy-stop ${trigger:.2f}  stop ${stop:.2f}  tp ${target:.2f} ({self.a.tp:g}R)  "
                f"{qty} sh{tag}")
        if self.a.arm:
            pend["trades"] = self.place_bracket(sym, pend)
            self._say(f"  🛡️ PRE-ARMED   {B.ts[-1]:%m-%d %H:%M}  {line}")
        else:
            self._say(f"  🛡️ WOULD PRE-ARM {B.ts[-1]:%m-%d %H:%M}  {line}   [shadow]")
        self.pending[sym] = pend

    def update_pending(self, sym: str, st, B: Bars) -> None:
        pend = self.pending[sym]
        new_stop = round(st.stop, 2)
        if new_stop > pend["stop"] - 0.005:          # stops only move DOWN; ignore sub-cent noise
            return
        if pend["trades"] and self._entry_filled(pend)[0] > 0:
            return                                   # already (partly) ours: reconcile settles it, never re-place
        risk = pend["trigger"] - new_stop
        eq = self.equity()
        qty = max(1, int((self.a.risk * eq) / risk)) if eq > 0 else pend["qty"]
        pend.update(stop=new_stop, qty=qty, target=round(pend["trigger"] + self.a.tp * risk, 2))
        self._say(f"  🔧 {B.ts[-1]:%m-%d %H:%M}  {sym} {pend['tf']} stop lowered -> ${new_stop:.2f}, "
                  f"{qty} sh, tp ${pend['target']:.2f}")
        if pend["trades"]:
            # OCA children cannot be revised in place (I-4). The bracket is unfilled, so cancel
            # it whole and re-place it under a NEW ref — a new OCA group. If the old parent fills
            # inside the cancel's round trip, the two brackets never share exits, and the fill
            # logger cancels the successor.
            old = pend["ref"]
            if not self._cancel_parent(pend, "re-place at the lower stop"):
                return
            pend["gen"] += 1
            pend["ref"] = f"{pend['ref0']}-r{pend['gen']}"
            self._superseded[old] = pend["ref"]
            pend["trades"] = self.place_bracket(sym, pend, event="REPLACED")
            self._say(f"  🔁 bracket re-placed as {pend['ref']} (OCA cannot be revised)")

    def cancel_pending(self, sym: str, reason: str, ts=None) -> None:
        pend = self.pending.pop(sym)
        if pend["trades"]:
            self._cancel_parent(pend, reason)
        when = f"{ts:%m-%d %H:%M}  " if ts else ""
        self._say(f"  🗑️ CANCEL {when}{sym} {pend['tf']} resting bracket — {reason}")

    def reconcile(self, sym: str, tf: str, B: Bars) -> None:
        """Per closed bar of THIS timeframe: sync the symbol's one resting setup with the strategy."""
        if sym in self.blocked:
            return
        pend = self.pending.get(sym)
        if pend and pend["tf"] != tf:
            return                                   # the slot is held on another timeframe (one per symbol)
        scan = self.strategy.scan(B, self._memo.setdefault((sym, tf), {}))
        n = len(B)
        if pend:
            st = scan.get(pend["key"])
            bar_entered = st is not None and st.state == ENTERED
            if self.a.arm and pend["trades"]:
                # ARMED: "entered" is what the broker FILLED. Stops trigger on quotes, not on a
                # print, so a bar can cross the trigger with no fill — and trusting the bar left
                # a ghost bracket resting and a second one armed on the next setup (I-5).
                filled, qty = self._entry_filled(pend)
                if filled >= qty:
                    self._settle_entry(sym, tf, pend, st, B, filled)
                    return
                if bar_entered or filled > 0:
                    pend["grace"] += 1               # one bar of grace: a fill can lag the bar by seconds
                    if pend["grace"] <= 1:
                        return
                    self._cancel_parent(pend, "grace over — a remainder never rests as a ghost")
                    if filled > 0:
                        self._say(f"  ⚠️ {B.ts[-1]:%m-%d %H:%M}  {sym} {tf} PARTIAL entry {filled}/{qty} sh — "
                                  f"remainder cancelled, the exits cover the filled shares")
                        self._settle_entry(sym, tf, pend, st, B, filled)
                    else:
                        self.pending.pop(sym)
                        self._missed_fill(sym, tf, pend, B)
                    return
            if st is None or st.state == DEAD:
                self.cancel_pending(sym, "setup invalidated", B.ts[-1])
            elif bar_entered:
                self.entered_at[(sym, tf)].add(st.entry_bar)
                est = max(pend["trigger"], B.o[-1])  # a gap over the open fills at the open
                if not self.a.arm:
                    self._say(f"  💥 {B.ts[-1]:%m-%d %H:%M}  {sym} {tf} WOULD FILL entry ≈ ${est:.2f} "
                              f"(trigger ${pend['trigger']:.2f}, bar open ${B.o[-1]:.2f})   [shadow]")
                    self.paper.append(dict(sym=sym, tf=tf, entry=est, trigger=pend["trigger"],
                                           stop=pend["stop"], target=pend["target"], ts=B.ts[-1], hi=est))
                self.pending.pop(sym)
            else:
                self.update_pending(sym, st, B)
        else:
            # NO ARMING AFTER THE FLATTEN (I-7). Higher-timeframe buckets aggregated from the
            # base stream can flush AFTER the day's flatten; arming off them would leave a fresh
            # DAY order with nothing scheduled to flatten it. Replay is exempt on purpose: it
            # walks symbols one after another, so a later symbol's whole day arrives after the
            # first symbol's EOD.
            if self.eod_done and not self.a.replay:
                return
            for key, st in sorted(scan.items()):     # the oldest armable FORMING setup
                if st.state == FORMING and n >= st.earliest and st.stop < float("inf"):
                    self.arm_pending(sym, tf, key, st, B)
                    break

    def _settle_entry(self, sym, tf, pend, st, B, filled) -> None:
        """The broker filled (part of) the entry: the bracket's exits own the position now."""
        bar_entered = st is not None and st.state == ENTERED
        self.entered_at[(sym, tf)].add(st.entry_bar if bar_entered else len(B) - 1)
        self.pending.pop(sym, None)
        if not bar_entered:
            self._say(f"  💥 {B.ts[-1]:%m-%d %H:%M}  {sym} {tf} entry FILLED ({filled} sh) before the bar "
                      f"showed the cross (quote-triggered) — exits resting")

    def _missed_fill(self, sym, tf, pend, B) -> None:
        """The bar crossed the trigger and the broker filled nothing. Recorded as a cost-model
        datapoint (leg MISSED in the fills CSV) — and never left resting."""
        hi = max(B.h[-2:]) if len(B) >= 2 else B.h[-1]
        self._say(f"  ❌ MISSED FILL {B.ts[-1]:%m-%d %H:%M}  {sym} {tf} bar high ${hi:.2f} crossed trigger "
                  f"${pend['trigger']:.2f}, broker filled 0 of {pend['qty']} sh — bracket cancelled")
        _append_csv(self.fills_csv, FILLS_HEADER,
                    f"{B.ts[-1]},{sym},{pend['ref']},MISSED,BUY,0,{pend['trigger']:.2f},{hi:.2f},")

    # ------------------------------------------------------------------ safety
    def _child_live(self, t) -> bool:
        """An exit child protects something only once its parent has filled. Before that it
        belongs to a RESTING bracket and must be neither counted as protection nor cancelled
        as an orphan."""
        pid = int(getattr(t.order, "parentId", 0) or 0)
        if not pid:
            return True
        par = self._parent_trades.get(pid)
        return par is None or int(getattr(par.orderStatus, "filled", 0) or 0) > 0

    def guard_brackets(self) -> None:
        """The watchdog, every 30 s on the MAIN thread (never inside a stream callback, I-2).
        Invariants: every share on MY book has a working stop; no exit order is larger than
        the book (an oversized fill flips the position short); a flat book has no exit orders
        left. Own book only (I-6). Repairs are fresh orders placed INSIDE the ref's OCA group,
        because the existing children cannot be revised (I-4)."""
        if not self.a.arm or self.a.replay:
            return
        try:
            working = [t for t in self.ib.openTrades() if t.orderStatus.status in WORKING_STATES]
            acct = {p.contract.symbol: int(p.position) for p in self.ib.positions()
                    if p.contract.symbol in self.contracts}
        except Exception as ex:
            self._say(f"  ⚠️ guard: could not read positions/orders ({ex})")
            return
        own, level, ref_of = defaultdict(int), {}, {}
        for ref, p in self.open_real.items():
            if p["qty"] > 0:
                own[p["sym"]] += p["qty"]
                if p["sym"] not in level or p["stop"] > level[p["sym"]]:   # the tightest stop covers all
                    level[p["sym"]], ref_of[p["sym"]] = p["stop"], ref
        mine = [t for t in working if t.order.orderRef in self.brackets and t.order.action == "SELL"
                and t.order.orderType in ("STP", "LMT") and self._child_live(t)]
        for sym in sorted(set(own) | {t.contract.symbol for t in mine}):
            q = own.get(sym, 0)
            sells = [t for t in mine if t.contract.symbol == sym]
            if q <= 0:
                for t in sells:
                    self._cancel_order(t.order)
                if sells:
                    self._say(f"  🧹 guard: {sym} book is flat — cancelled {len(sells)} orphan exit order(s)")
                continue
            stops = [t for t in sells if t.order.orderType == "STP"]
            tps = [t for t in sells if t.order.orderType == "LMT"]
            remaining = lambda t: int(t.order.totalQuantity - (t.orderStatus.filled or 0))
            stop_qty, tp_qty = sum(map(remaining, stops)), sum(map(remaining, tps))
            ref = ref_of[sym]
            if stop_qty != q:
                if stop_qty > q:                     # oversized stop: a fill would flip the book short
                    for t in stops:
                        self._cancel_order(t.order)
                    stop_qty = 0
                miss = q - stop_qty
                o = self.Order(action="SELL", orderType="STP", totalQuantity=miss, auxPrice=level[sym],
                               tif="DAY", orderRef=ref, ocaGroup=ref, ocaType=2)
                tr = self.ib.placeOrder(self.contracts[sym], o)
                tr.fillEvent += self._fill_logger("SL")
                self._say(f"  🚨 guard: {sym} book {q} sh had {stop_qty} sh of working stop — placed a stop "
                          f"for {miss} @ ${level[sym]:.2f} inside {ref}'s OCA group")
            if tp_qty > q:
                for t in tps:
                    self._cancel_order(t.order)
                self._say(f"  🚨 guard: {sym} take-profit size {tp_qty} > book {q} — cancelled; the stop stays")
        for sym, aq in sorted(acct.items()):
            if aq != own.get(sym, 0) and (sym, aq) not in self._acct_noted:
                self._acct_noted.add((sym, aq))
                self._say(f"  ℹ️ {sym}: account shows {aq:+d} sh, my book {own.get(sym, 0)} — the difference "
                          f"belongs to another strategy or a previous run; not mine to touch")

    def flatten(self, reason: str) -> None:
        """Cancel MY working orders and close MY book — nothing else. Never a global cancel:
        that kills other strategies' orders (I-6). Idempotent inside a short window: closes
        fill in milliseconds but position reports lag, and a re-fired flatten once oversold
        a book into a short (I-2). One market close per ref, routed on our own qualified
        contract (I-1), confirmed later by verify_flatten()."""
        if self._last_flatten and _time.time() - self._last_flatten < FLATTEN_WINDOW_S:
            self._say(f"  ↩️ flatten ({reason}) skipped — one is already in flight")
            return
        self._last_flatten = _time.time()
        n_cancel = 0
        try:
            for t in self.ib.openTrades():
                if t.order.orderRef in self.brackets and t.orderStatus.status not in DONE_STATES:
                    n_cancel += self._cancel_order(t.order)
        except Exception as ex:
            self._say(f"  ⚠️ could not list my open orders to cancel them: {ex}")
        for sym in list(self.pending):               # a bracket the broker has not echoed yet
            pend = self.pending.pop(sym)
            if pend["trades"]:
                self._cancel_parent(pend, reason)
        now = self._last_t or datetime.now(ET).replace(tzinfo=None)
        if not self.a.arm and self.paper:            # SHADOW: close simulated positions at the last price
            for p in self.paper:
                B = self.store.get((p["sym"], p["tf"]))
                px = B.c[-1] if B and len(B) else p["trigger"]
                self._book_shadow_close(p, px, now, f"WOULD CLOSE ({reason})")
            self.paper = []
        closes = []
        if self.a.arm:
            for ref, p in list(self.open_real.items()):
                if p["qty"] <= 0:
                    continue
                inflight = self._inflight.get(ref)
                if inflight is not None and inflight.orderStatus.status not in DONE_STATES:
                    self._say(f"  ↩️ {p['sym']}: a close is already working ({inflight.orderStatus.status}) "
                              f"— not placing another")
                    continue
                c = self.contracts.get(p["sym"])
                if c is None:
                    from ib_async import Stock          # live-only dependency
                    c = Stock(p["sym"], "SMART", "USD")  # SMART — never the listing exchange (I-1)
                    try:
                        self.ib.qualifyContracts(c)
                    except Exception:
                        pass
                o = self.MarketOrder("SELL", p["qty"])
                o.orderRef = ref                     # attributable in the executions forever
                tr = self.ib.placeOrder(c, o)
                tr.fillEvent += self._fill_logger("EOD")
                self._inflight[ref] = tr
                closes.append((p["sym"], p["qty"], tr))
            own = defaultdict(int)
            for p in self.open_real.values():
                own[p["sym"]] += p["qty"]
            try:
                for pos in self.ib.positions():
                    sym = pos.contract.symbol
                    if pos.position != 0 and sym in self.contracts and int(pos.position) != own.get(sym, 0):
                        self._say(f"  🚨 {sym}: account holds {pos.position:+.0f} sh, my book {own.get(sym, 0)} — "
                                  f"not mine, not touching it. If it is yours, CLOSE IT MANUALLY")
            except Exception as ex:
                self._say(f"  ⚠️ could not read account positions: {ex}")
        self._say(f"  ⛔ FLATTEN ({reason}) — cancelled {n_cancel} of my order(s), closing {len(closes)} "
                  f"position(s) ({sum(q for _, q, _ in closes)} sh) — own book only")
        self._closes = closes
        if closes:
            self._defer(VERIFY_DELAY_S, lambda: self.verify_flatten(1))

    def _book_shadow_close(self, p: dict, px: float, ts, what: str) -> None:
        risk = p["trigger"] - p["stop"]
        r = (px - p["entry"]) / risk if risk > 0 else 0.0
        mfe = (p.get("hi", p["entry"]) - p["entry"]) / risk if risk > 0 else 0.0
        self._say(f"  🕓 {what} {p['sym']} {p['tf']} @ ${px:.2f}  ({r:+.2f}R vs entry ${p['entry']:.2f})   [shadow]")
        self.closed.append(dict(ts=ts, sym=p["sym"], tf=p["tf"], kind="EOD", exit=px, r=r,
                                entry=p["entry"], trigger=p["trigger"], stop=p["stop"],
                                entry_ts=p.get("ts"), mfe=mfe))

    def _defer(self, seconds: float, fn) -> bool:
        """Run fn after `seconds` WITHOUT blocking. Inside the broker's event loop (every stream
        callback) a blocking sleep raises 'event loop is already running' — and the event
        library swallows it silently, so the confirmation never runs (I-2). Schedule on the
        running loop instead. Returns False when no loop is running (the Ctrl-C path, tests):
        the caller then sleeps and calls verify_flatten itself."""
        import asyncio
        try:
            asyncio.get_running_loop().call_later(seconds, fn)
            return True
        except RuntimeError:
            return False

    def verify_flatten(self, attempt: int = VERIFY_ATTEMPTS) -> bool:
        """A flatten is real only once the broker confirms it. A close that is still WORKING
        is re-checked (a thin book fills a market close in pieces) before it is called a
        failure — the first version shouted CLOSE MANUALLY at closes that filled a second
        later (I-9). Returns True when nothing is left to wait for."""
        closes, self._closes = self._closes, []
        waiting, bad = [], []
        for sym, qty, tr in closes:
            st = tr.orderStatus.status
            if st == "Filled":
                self._say(f"  ✅ close {sym} {qty:+d} sh: Filled")
            elif st in WORKING_STATES and attempt < VERIFY_ATTEMPTS:
                waiting.append((sym, qty, tr))
            else:
                filled = int(getattr(tr.orderStatus, "filled", 0) or 0)
                why = f" — {tr.log[-1].message}" if getattr(tr, "log", None) else ""
                self._say(f"  🚨 close {sym} {qty:+d} sh: {st} ({filled}/{qty} filled){why}")
                bad.append(f"{sym} {qty - filled:+d}")
        if waiting:
            self._say(f"  ⏳ {len(waiting)} close(s) still working — re-checking in {VERIFY_DELAY_S}s "
                      f"(attempt {attempt + 1}/{VERIFY_ATTEMPTS})")
            self._closes = waiting
            self._defer(VERIFY_DELAY_S, lambda: self.verify_flatten(attempt + 1))
            return False
        for ref, p in self.open_real.items():        # on the book with no close working at all
            if p["qty"] > 0 and self._inflight.get(ref) is None:
                bad.append(f"{p['sym']} {p['qty']:+d} (no close placed)")
        if bad:
            self._say("  🚨 STILL OPEN after flatten — CLOSE MANUALLY NOW: " + ", ".join(bad))
        return True

    # ------------------------------------------------------------------ bar pipeline
    def on_closed_bar(self, sym, tf, t, o, h, l, c, v) -> bool:
        """One CLOSED bar of any timeframe: append it, then run the setup lifecycle and the
        referee for that series. Returns True if the bar was new."""
        if self._report and self.cur_day is not None and t.date() < self.cur_day:
            return False                             # a reconnect re-delivering an OLDER session
        key = (sym, tf)
        B = self.store.get(key)
        if B is None or B.date != t.date():
            B = Bars(symbol=sym, date=t.date(), timeframe=tf)
            self.store[key] = B
            self.fired[key] = set()
            self._memo[key] = {}                     # a new session: nothing is final yet
        if B.ts and t <= B.ts[-1]:
            return False                             # already ingested (reconnects replay bars)
        B.append(t, o, h, l, c, v)
        if tf == self.base_tf and t.date() != self.cur_day:
            self.cur_day = t.date()
            self.eod_done = False
        if (self._report and tf == self.base_tf and len(B) == 1 and sym in self.unscreened
                and self.universe_hook is not None):
            self.unscreened.discard(sym)             # admitted pre-open: judged on the session's open (I-8)
            try:
                self.universe_hook(sym, o)
            except Exception as ex:
                self._say(f"  ⚠️ universe re-check failed for {sym}: {ex}")
            if sym in self.blocked:
                return True
        if self._report and tf == self.base_tf and not self.a.replay:   # live proof of life:
            self._last_t = t                                            # silence must never be ambiguous
            if not self.live_started:
                self.live_started = True
                self._say(f"  ▶️ {t:%m-%d %H:%M} first LIVE bar ({sym} ${c:.2f}) — stream confirmed")
            self._live_bars += 1
            if self.last_beat is None or (t - self.last_beat).total_seconds() >= HEARTBEAT_S:
                self.last_beat = t
                self._say(f"  ❤️ {t:%m-%d %H:%M} alive — {self._live_bars} live bars, "
                          f"{len(self.pending)} resting setup(s)")
            self._draw()
        if self._report and not self.a.arm and self.paper:
            # SHADOW exits, labeler conventions: checked from the bar AFTER entry, on the
            # position's own timeframe, and the stop wins when one bar touches both.
            keep = []
            for p in self.paper:
                if p["sym"] != sym or p["tf"] != tf:
                    keep.append(p)
                    continue
                risk = p["trigger"] - p["stop"]
                p["hi"] = max(p.get("hi", p["entry"]), h)      # the peak BEFORE the exit check
                mfe = (p["hi"] - p["entry"]) / risk if risk > 0 else 0.0
                if l <= p["stop"]:
                    r = (p["stop"] - p["entry"]) / risk if risk > 0 else 0.0
                    self._say(f"  🩸 {t:%m-%d %H:%M}  {sym} {tf} WOULD STOP OUT @ ${p['stop']:.2f}  "
                              f"({r:+.2f}R, peaked +{mfe:.2f}R)   [shadow]")
                    self.closed.append(dict(ts=t, sym=sym, tf=tf, kind="STOP", exit=p["stop"], r=r,
                                            entry=p["entry"], trigger=p["trigger"], stop=p["stop"],
                                            entry_ts=p.get("ts"), mfe=mfe))
                elif h >= p["target"]:
                    r = (p["target"] - p["entry"]) / risk if risk > 0 else 0.0
                    self._say(f"  🎯 {t:%m-%d %H:%M}  {sym} {tf} WOULD TAKE PROFIT @ ${p['target']:.2f}  "
                              f"({r:+.2f}R, peaked +{mfe:.2f}R)   [shadow]")
                    self.closed.append(dict(ts=t, sym=sym, tf=tf, kind="TP", exit=p["target"], r=r,
                                            entry=p["entry"], trigger=p["trigger"], stop=p["stop"],
                                            entry_ts=p.get("ts"), mfe=mfe))
                else:
                    keep.append(p)
            self.paper = keep
        if t.time() >= self.a.eod:
            if self._report and not self.eod_done:
                self.eod_done = True                 # set FIRST: an exception inside flatten must never re-fire it
                self.flatten(f"EOD {self.a.eod:%H:%M}")
            elif self._report and not self.a.arm:
                # replay walks symbols one at a time: later symbols reach their own EOD after the
                # global flatten already fired -> close THIS symbol's shadow day here.
                if sym in self.pending:
                    self.cancel_pending(sym, "EOD — unfilled", t)
                keep = []
                for p in self.paper:
                    if p["sym"] != sym:
                        keep.append(p)
                        continue
                    B2 = self.store.get((p["sym"], p["tf"]))
                    px = B2.c[-1] if B2 and len(B2) else p["trigger"]
                    self._book_shadow_close(p, px, t, f"WOULD CLOSE (EOD {self.a.eod:%H:%M})")
                self.paper = keep
            return True
        if self._report:
            self.reconcile(sym, tf, B)
            if not self.a.replay:                    # live: referee every bar; replay: once per day
                self.referee_once(sym, tf)
        return True

    def referee_once(self, sym: str, tf: str) -> None:
        """Cross-check every entry the strategy's INDEPENDENT batch detector finds against
        what the pre-armer actually did. Two implementations, one truth."""
        key = (sym, tf)
        B = self.store.get(key)
        if not B or not len(B):
            return
        # Every entry is kept, not just the latest: replay runs the referee once per session,
        # and a single "last entry" slot labelled every earlier pre-armed entry as missed.
        for e in sorted(self.strategy.detect(B), key=lambda e: e.bar):
            if e.bar in self.fired[key]:
                continue
            self.fired[key].add(e.bar)
            tag = ("pre-armed ✓" if e.bar in self.entered_at.get(key, ())
                   else "NOT pre-armed (occupied / at cap / seeded mid-setup)")
            self._say(f"  📋 {B.ts[e.bar]:%m-%d %H:%M}  detector confirms entry {sym} {tf} "
                      f"${e.price:.2f} — {tag}")

    def feed_bar(self, sym, t, o, h, l, c, v) -> None:
        """ONE closed base bar from ANY source -> the base pipeline, then the local k-minute
        aggregators. Duplicates are dropped, so overlapping sources are safe."""
        if not (RTH_OPEN <= t.time() <= RTH_CLOSE) or sym in self.blocked:
            return
        if not self.on_closed_bar(sym, self.base_tf, t, o, h, l, c, v):
            return
        for k in self.agg_ks:
            agg = self.aggs.get((sym, k))
            if agg is None:
                agg = self.aggs[(sym, k)] = MinuteAggregator(
                    k, (lambda s_, k_: lambda ts, o_, h_, l_, c_, v_:
                        self.on_closed_bar(s_, f"{k_}min", ts, o_, h_, l_, c_, v_))(sym, k))
            agg.add(t, o, h, l, c, v)

    def ingest(self, sym: str, bar_list, report: bool) -> None:
        """Feed a broker / cached bar list. The final bar is skipped: when live it is still forming."""
        self._report = report
        for b in bar_list[:-1]:
            t = b.date.astimezone(ET).replace(tzinfo=None) if hasattr(b.date, "astimezone") else b.date
            self.feed_bar(sym, t, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume))
