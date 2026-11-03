"""
flatten_leftovers.py — start every session flat
===============================================
    python flatten_leftovers.py                        # DRY RUN: prints what it would close
    python flatten_leftovers.py --execute              # place the closing orders
    python flatten_leftovers.py --execute --exclude XYZ
    python flatten_leftovers.py --execute --cancel-duplicates

Every strategy flattens its own book at the end of the day, but a close the broker never
confirmed survives the night (I-1, I-2), and so does a position a buggy process believed
it had already exited (I-11). A leftover corrupts the next day's evaluation twice: its
P&L lands in a session that did not choose the trade, and its shares sit in the account
while today's sizing is computed. Run this before the open so the day's record contains
only the day's trading. A market order placed pre-market queues for the opening print —
the cleanest possible boundary between yesterday and today.

SAFETY
  * DRY RUN by default. Nothing is placed without --execute.
  * A symbol with ANY execution today is live trading, not a leftover, and is skipped
    (--force overrides). Run by accident at 11:00, it refuses to touch the day's trades.
  * Never two closes against one position (I-10). Working closes are counted first —
    through reqAllOpenOrders, because openTrades() only sees THIS client id's orders and a
    second run cannot otherwise see the first run's — and only the shortfall is placed.
    An oversized total is reported; --cancel-duplicates trims it.
  * Routed SMART, never on the listing exchange the positions report carries (I-1).
  * Every close is read back; anything still open is shouted, never assumed done.
  * Never a global cancel: other strategies' resting orders are not this tool's to kill.
  * Paper ports only. orderRef `cleanup-<date>`, so these fills are attributable forever and
    can never be mistaken for a strategy's trade.
  * Every run appends data/cleanup_log.csv: a leftover's cash has to land somewhere.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, time as dtime

from harness.bars import ET
from harness.config import LIVE_PORTS, PAPER_PORT, PATHS
from harness.engine import WORKING_STATES


def leftovers(positions: dict, traded_today: set, exclude=()) -> list[tuple]:
    """What to close. PURE, so the rules are testable without a broker.
    positions {symbol: qty} (qty < 0 = short) -> [(symbol, qty, action, skip_reason)];
    skip_reason '' means close it."""
    out = []
    for sym in sorted(positions):
        qty = int(positions[sym])
        if qty == 0:
            continue
        action = "SELL" if qty > 0 else "BUY"        # a short closes by BUYING
        if sym in exclude:
            why = "excluded by --exclude"
        elif sym in traded_today:
            why = "traded TODAY — not a leftover (--force to close anyway)"
        else:
            why = ""
        out.append((sym, qty, action, why))
    return out


def open_orders(ib) -> list:
    """Every working order on the ACCOUNT, whichever client id placed it."""
    try:
        ib.reqAllOpenOrders()
        ib.sleep(1)
    except Exception:
        pass
    return [t for t in ib.openTrades() if t.orderStatus.status in WORKING_STATES]


def working_closes(orders: list, sym: str, action: str) -> tuple[int, list]:
    """(shares already working in the closing direction for sym, those orders)."""
    live = [t for t in orders if t.contract.symbol == sym and t.order.action == action]
    return sum(int(t.order.totalQuantity - (t.orderStatus.filled or 0)) for t in live), live


def record(rows: list[dict], path: str = PATHS.cleanup) -> None:
    """Append — never overwrite. This is money."""
    cols = ["time", "symbol", "qty", "action", "status", "avg_fill", "ref", "note"]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", restval="")
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"  📒 recorded -> {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Close positions left over from a previous session.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=PAPER_PORT)
    ap.add_argument("--client-id", type=int, default=70, help="must not clash with a running engine")
    ap.add_argument("--execute", action="store_true", help="place the closing orders (default: dry run)")
    ap.add_argument("--exclude", default="", help="comma-separated symbols to leave alone")
    ap.add_argument("--force", action="store_true", help="close even symbols traded today (DANGEROUS mid-session)")
    ap.add_argument("--cancel-orders", action="store_true", help="also cancel resting orders on leftover symbols")
    ap.add_argument("--cancel-duplicates", action="store_true", help="trim working closes that exceed the position")
    ap.add_argument("--i-understand-live", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.port in LIVE_PORTS and not a.i_understand_live:
        sys.exit(f"✗ port {a.port} is a LIVE trading port — this tool is paper-only. Refusing.")
    exclude = {s.strip().upper() for s in a.exclude.split(",") if s.strip()}

    try:
        from ib_async import IB, Stock, MarketOrder, ExecutionFilter
    except ImportError:
        sys.exit("✗ ib_async is not installed:  pip install ib_async")
    ib = IB()
    try:
        ib.connect(a.host, a.port, clientId=a.client_id, timeout=10)
    except Exception as e:
        sys.exit(f"✗ cannot reach IB Gateway/TWS at {a.host}:{a.port} ({e})")

    now = datetime.now(ET)
    today = now.date().isoformat()
    print(f"LEFTOVER FLATTEN — {'🔴 EXECUTING' if a.execute else '🟢 DRY RUN (nothing will be placed)'}"
          f"  ·  {now:%Y-%m-%d %H:%M:%S} ET")
    pos = {p.contract.symbol: int(p.position) for p in ib.positions() if p.position != 0}
    cost = {p.contract.symbol: float(p.avgCost) for p in ib.positions() if p.position != 0}
    if not pos:
        print("\n  ✅ the account is FLAT — nothing left over, the session starts clean.")
        ib.disconnect()
        return

    traded_today = set()
    try:
        fills = ib.reqExecutions(ExecutionFilter())
        ib.sleep(2)
        traded_today = {f.contract.symbol for f in fills
                        if f.execution.time.astimezone(ET).date().isoformat() == today}
    except Exception as e:
        print(f"  ⚠️ could not read today's executions ({e}) — treating every position as a leftover")
    if a.force:
        traded_today = set()

    plan = leftovers(pos, traded_today, exclude)
    print(f"\n  {'sym':<7}{'qty':>7}{'avg cost':>11}  action")
    for sym, qty, action, why in plan:
        print(f"  {'→' if not why else '·'} {sym:<7}{qty:>7}{cost.get(sym, 0):>11.2f}  "
              + (f"{action} {abs(qty)} to close" if not why else f"SKIP — {why}"))
    doomed = [(s, q, act) for s, q, act, why in plan if not why]
    if not doomed:
        print("\n  nothing to close — every open position is today's or excluded.")
        ib.disconnect()
        return

    orders = open_orders(ib)
    if a.cancel_orders:
        syms = {s for s, _, _ in doomed}
        for t in [t for t in orders if t.contract.symbol in syms]:
            verb = "cancelled" if a.execute else "would cancel"
            if a.execute:
                try:
                    ib.cancelOrder(t.order)
                except Exception as e:
                    print(f"  ⚠️ cancel failed for {t.contract.symbol}: {e}")
                    continue
            print(f"  🗑️ {verb} resting {t.order.action} {t.contract.symbol} ({t.order.orderRef or 'no ref'})")
        if a.execute:
            orders = open_orders(ib)
    if not a.execute:
        print(f"\n  🟢 DRY RUN — {len(doomed)} position(s) would be closed. Re-run with --execute.")
        ib.disconnect()
        return
    if now.time() < dtime(9, 30):
        print("\n  ⏰ before the open — market orders QUEUE and fill at the opening print.")

    ref = f"cleanup-{today}"
    stamp = f"{now:%Y-%m-%d %H:%M:%S}"
    placed, rows = [], []
    for sym, qty, action in doomed:
        rest, live = working_closes(orders, sym, action)
        if rest > abs(qty):
            print(f"  🚨 {sym}: {rest} sh of closing orders already working against a {abs(qty)} sh "
                  f"position — filling them all would REVERSE it.")
            if a.cancel_duplicates:
                for t in live:
                    if rest <= abs(qty):
                        break
                    try:
                        ib.cancelOrder(t.order)
                        rest -= int(t.order.totalQuantity - (t.orderStatus.filled or 0))
                        print(f"     🗑️ cancelled duplicate {t.order.action} {t.order.totalQuantity:.0f} {sym}")
                    except Exception as e:
                        print(f"     ⚠️ cancel failed: {e}")
                rows.append({"time": stamp, "symbol": sym, "qty": qty, "action": action, "ref": ref,
                             "status": "DUPLICATES CANCELLED", "note": f"{rest} sh left working"})
            else:
                print(f"     re-run with --cancel-duplicates to trim it to {abs(qty)} sh")
            continue
        need = abs(qty) - rest
        if need <= 0:
            print(f"  ↩️ {sym}: {rest} sh already working to close — placing nothing")
            rows.append({"time": stamp, "symbol": sym, "qty": qty, "action": action, "ref": ref,
                         "status": "ALREADY WORKING", "note": f"{rest} sh resting"})
            continue
        c = Stock(sym, "SMART", "USD")               # SMART — never the listing exchange (I-1)
        try:
            ib.qualifyContracts(c)
            if not getattr(c, "conId", 0):
                raise ValueError("unknown contract")
        except Exception as e:
            print(f"  🚨 {sym}: cannot qualify ({e}) — CLOSE THIS ONE MANUALLY")
            rows.append({"time": stamp, "symbol": sym, "qty": qty, "action": action, "ref": ref,
                         "status": "NOT PLACED", "note": f"qualify failed: {e}"})
            continue
        o = MarketOrder(action, need)
        o.orderRef, o.tif = ref, "DAY"
        placed.append((sym, qty, ib.placeOrder(c, o)))
        print(f"  ⛔ {action} {need} {sym} placed (ref {ref})" + (f" — {rest} sh were already working" if rest else ""))

    ib.sleep(5)                                      # a standalone script: a blocking sleep is safe here
    print()
    for sym, qty, tr in placed:
        st = tr.orderStatus.status
        avg = float(tr.orderStatus.avgFillPrice or 0)
        mark = "✅" if st == "Filled" else "⏳" if st in WORKING_STATES else "🚨"
        print(f"  {mark} {sym} {qty:+d}: {st}{f' @ ${avg:.2f}' if avg else ''}")
        rows.append({"time": f"{datetime.now(ET):%Y-%m-%d %H:%M:%S}", "symbol": sym, "qty": qty,
                     "action": "SELL" if qty > 0 else "BUY", "status": st, "ref": ref,
                     "avg_fill": f"{avg:.4f}" if avg else "",
                     "note": "queued for the open" if st in WORKING_STATES else ""})
    record(rows)
    left = {p.contract.symbol: int(p.position) for p in ib.positions() if p.position != 0}
    still = {s: q for s, q in left.items() if s in {d[0] for d in doomed}}
    if still:
        print("\n  ⏳ still open — queued for the open, or rejected. Re-run after 09:31 and CHECK MANUALLY "
              "if it persists: " + ", ".join(f"{s} {q:+d}" for s, q in still.items()))
    else:
        print("\n  ✅ every leftover is closed — the account is flat for the session.")
    ib.disconnect()


if __name__ == "__main__":
    main()
