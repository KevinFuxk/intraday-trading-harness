"""
real_ledger.py — the trades the market actually gave, rebuilt from the broker's executions
==========================================================================================
    python real_ledger.py 2025-03-03

A replay records what the rules SAID a session was worth. This records what the broker
DID: real entry and exit fills (partials volume-weighted), the leg that closed each trade,
R measured against the bracket's own trigger and stop, and the highest R the trade reached
(peak R, from the cached bars). It lands in the same ledger as the replay, variant
`<strategy>:armed`, so replay and reality sit one filter apart.

Sources, all point-in-time:
  * the broker's executions — served for the CURRENT day only, so they are saved to
    data/executions/<day>.csv the first time and read from disk forever after
  * data/brackets.csv — the levels each order ref was placed at (written by the engine)
  * data/fills.csv    — which bracket leg fired (TP / SL / EOD)
  * the bar cache     — the path between entry and exit, for peak R

Trades are keyed by ORDER REF, so two strategies on one account never mix. Executions that
carry no ref (a manual close, a previous run's leftovers) are matched FIFO to open entries of
the same symbol where possible; anything left over is reported as CHURN rather than dropped —
an account that does not reconcile to its ledger should say so out loud.
"""
from __future__ import annotations

import csv
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, time as dtime

from harness import ledger
from harness.bars import ET, load_bars
from harness.config import PATHS, PAPER_PORT

REF = re.compile(r"(?P<strategy>[a-z0-9]+)-(?P<sym>[A-Z.]+)-(?P<tf>\w+)-\d{4}-\d\d-\d\d-k(?P<key>\d+)(-r\d+)?$")
EXEC_COLS = ["time", "symbol", "side", "shares", "price", "ref", "exec_id"]


def load_executions(day: str, port: int = PAPER_PORT) -> list[dict]:
    path = os.path.join(PATHS.executions, f"{day}.csv")
    if os.path.exists(path):
        return list(csv.DictReader(open(path)))
    if day != datetime.now(ET).date().isoformat():
        sys.exit(f"✗ the broker only serves today's executions, and {path} was never saved")
    from ib_async import IB, ExecutionFilter
    ib = IB()
    ib.connect("127.0.0.1", port, clientId=54, timeout=10)
    fills = ib.reqExecutions(ExecutionFilter())
    ib.sleep(2)
    ib.disconnect()
    rows = []
    for f in sorted(fills, key=lambda f: f.execution.time):
        e = f.execution
        t = e.time.astimezone(ET)
        if t.date().isoformat() == day:
            rows.append({"time": f"{t:%H:%M:%S}", "symbol": f.contract.symbol, "side": e.side,
                         "shares": f"{e.shares:.0f}", "price": f"{e.price:.4f}",
                         "ref": e.orderRef or "", "exec_id": e.execId})
    os.makedirs(PATHS.executions, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=EXEC_COLS)
        w.writeheader()
        w.writerows(rows)
    print(f"  saved {len(rows)} executions -> {path}")
    return rows


def bracket_levels(path: str = PATHS.brackets) -> dict[str, dict]:
    """order ref -> the trigger / stop it was placed at (the latest event for that ref)."""
    out = {}
    if os.path.exists(path):
        for r in csv.DictReader(open(path)):
            out[r["ref"]] = {"trigger": float(r["trigger"]), "stop": float(r["stop"])}
    return out


def leg_kinds(day: str, path: str = PATHS.fills) -> dict[str, set]:
    """order ref -> {'TP', 'SL', 'EOD'} legs that filled that day."""
    out: dict[str, set] = defaultdict(set)
    if os.path.exists(path):
        for r in csv.DictReader(open(path)):
            if r["time"].startswith(day):
                out[r["ref"]].add(r["leg"])
    return out


def _vwap(fills):
    q = sum(f[0] for f in fills)
    return (sum(f[0] * f[1] for f in fills) / q if q else 0.0), q


def reconstruct(executions: list[dict], levels: dict, legs: dict, peak_bars=None):
    """PURE: executions + bracket levels + leg kinds -> (trades, notes, churn).
    `peak_bars(sym)` may return a Bars series for peak R; None skips it."""
    by_ref, loose = defaultdict(list), []
    for r in executions:
        (by_ref[r["ref"]] if r["ref"] else loose).append(dict(r))
    loose.sort(key=lambda r: r["time"])
    used = [False] * len(loose)

    def take_loose(sym, after, qty):
        got, need = [], qty
        for i, r in enumerate(loose):
            if used[i] or r["symbol"] != sym or r["side"] != "SLD" or r["time"] < after:
                continue
            sh = min(int(r["shares"]), need)
            got.append((sh, float(r["price"]), r["time"]))
            if sh == int(r["shares"]):
                used[i] = True
            else:
                r["shares"] = str(int(r["shares"]) - sh)
            need -= sh
            if need <= 0:
                break
        return got

    trades, notes, foreign = [], [], defaultdict(int)
    for ref, fills in sorted(by_ref.items(), key=lambda kv: min(f["time"] for f in kv[1])):
        m = REF.match(ref)
        if not m:
            # A ref from another tool (a cleanup close, a manual ticket). Never silently dropped.
            for f in fills:
                foreign[f["symbol"]] += int(f["shares"]) * (1 if f["side"] == "BOT" else -1)
            notes.append(f"foreign ref {ref}: {len(fills)} execution(s), not a strategy bracket")
            continue
        sym, tf = m["sym"], m["tf"]
        buys = [(int(f["shares"]), float(f["price"]), f["time"]) for f in fills if f["side"] == "BOT"]
        sells = [(int(f["shares"]), float(f["price"]), f["time"]) for f in fills if f["side"] == "SLD"]
        if not buys:
            continue
        entry, qty = _vwap(buys)
        t_in = min(b[2] for b in buys)
        sold = sum(s[0] for s in sells)
        ks = legs.get(ref, set())
        leg = "+".join(n for n, k in (("TP", "TP"), ("STOP", "SL"), ("EOD", "EOD")) if k in ks)
        exits = list(sells)
        kind = leg or ("TP" if sells and _vwap(sells)[0] > entry else "STOP" if sells else "")
        if sold < qty:                               # the rest was closed by an unattributed order
            got = take_loose(sym, t_in, qty - sold)
            if not got and not exits:
                notes.append(f"{sym}: {ref} OPEN — no exit found for {qty} sh")
                continue
            exits += got
            if got:
                kind = leg + "+EOD" if (leg and "EOD" not in leg) else (leg or "EOD")
        exit_px, _ = _vwap(exits)
        t_out = max(e[2] for e in exits)
        lv = levels.get(ref)
        trigger, stop = (lv["trigger"], lv["stop"]) if lv else (entry, None)
        risk = (trigger - stop) if stop is not None else None
        peak = ""
        if risk and risk > 0 and peak_bars is not None:
            b = peak_bars(sym)
            if b is not None:
                t0 = dtime(int(t_in[:2]), int(t_in[3:5]))
                t1 = dtime(int(t_out[:2]), int(t_out[3:5]))
                his = [b.h[i] for i in range(len(b)) if t0 <= b.ts[i].time() <= t1]
                if his:
                    peak = f"{(max(max(his), entry) - entry) / risk:.2f}"
        trades.append({
            "symbol": sym, "tf": tf, "entry_time": t_in[:5], "entry": f"{entry:.2f}",
            "trigger": f"{trigger:.2f}", "stop": f"{stop:.2f}" if stop is not None else "",
            "stop_pct": f"{risk / trigger * 100:.2f}" if risk else "",
            "exit_time": t_out[:5], "exit_kind": kind, "exit": f"{exit_px:.2f}",
            "R": f"{(exit_px - entry) / risk:.2f}" if risk else "", "peak_R": peak,
            "_strategy": m["strategy"], "_qty": qty, "_pnl": (exit_px - entry) * qty})
    churn = defaultdict(lambda: [0, 0.0])
    for i, r in enumerate(loose):
        if not used[i] and int(r["shares"]) > 0:
            sign = 1 if r["side"] == "SLD" else -1
            churn[r["symbol"]][0] += -sign * int(r["shares"])
            churn[r["symbol"]][1] += sign * int(r["shares"]) * float(r["price"])
    for sym, q in foreign.items():
        churn[sym][0] += q
    return trades, notes, dict(churn)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: python real_ledger.py YYYY-MM-DD")
    day = sys.argv[1]
    ex = load_executions(day)
    trades, notes, churn = reconstruct(ex, bracket_levels(), leg_kinds(day),
                                       peak_bars=lambda sym: load_bars(sym, day, "1min") or load_bars(sym, day, "15s"))
    for strat in sorted({t["_strategy"] for t in trades}) or []:
        rows = [{k: v for k, v in t.items() if not k.startswith("_")} for t in trades if t["_strategy"] == strat]
        ledger.write_variant(day, f"{strat}:armed", rows)
    print(f"{day} — real fills: {len(trades)} trade(s)")
    tot_r = 0.0
    for t in trades:
        r = float(t["R"]) if t["R"] else 0.0
        tot_r += r
        print(f"  {t['entry_time']:>5} {t['symbol']:<6}{t['_qty']:>6}  {t['entry']:>9} -> {t['exit']:>9}  "
              f"{t['exit_kind']:<9}{r:>+6.2f}R  peak {t['peak_R'] or '-':>5}")
    print(f"  total {tot_r:+.2f}R")
    for n in notes:
        print(f"  ⚠️ {n}")
    for sym, (q, cash) in sorted(churn.items()):
        print(f"  ⚠️ churn {sym}: net {q:+d} sh, cash {cash:+,.0f} — not explained by any strategy bracket")


if __name__ == "__main__":
    main()
