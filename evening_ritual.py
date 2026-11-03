"""
evening_ritual.py — one command after the close records everything about the session
====================================================================================
    python evening_ritual.py                      # the last completed session, default strategy
    python evening_ritual.py --strategy macx --day 2025-03-03

  1. CACHE     the session's bars for every watchlist symbol (one broker request each)
  2. REPLAY    the session through the live code path, from disk, once per variant
               (with the minimum-stop gate and without — the gate's cost is a column, not a guess)
  3. REAL      rebuild the armed trades from the broker's executions (today only — this step
               cannot be done tomorrow)
  4. REPORT    regenerate data/ledger.html: per-variant statistics, the universe with every
               drop and its reason, and every recorded trade, filterable

Idempotent: re-running a session replaces that session's rows. Two of the four steps can
ONLY happen on the day itself (the broker serves executions for the current day, and the
cache is the only place the curated universe's history will ever exist) — which is why this
is one command, run every evening, instead of four that are easy to forget.
"""
from __future__ import annotations

import argparse
import csv
import html
import os
import subprocess
import sys
from collections import defaultdict

from cache_bars import last_session
from harness import ledger
from harness.config import EngineConfig, PATHS

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
HTML = "data/ledger.html"


def step(title: str, cmd: list[str], tail: int = 8, must: bool = False) -> int:
    print("=" * 72 + f"\n{title}")
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE)
    for line in r.stdout.splitlines()[-tail:]:
        print("  " + line)
    if r.returncode != 0:
        print(r.stderr[-800:])
        if must:
            sys.exit(f"✗ {title} failed — is IB Gateway running and logged in (paper)?")
    return r.returncode


def build_html(path: str = HTML) -> str:
    rows = ledger.read()
    variants = sorted({r["variant"] for r in rows})

    def stats(rs):
        if not rs:
            return "no trades yet"
        n, tot = len(rs), sum(float(r["R"] or 0) for r in rs)
        wins = sum(float(r["R"] or 0) > 0 for r in rs)
        kinds = defaultdict(int)
        for r in rs:
            kinds[r["exit_kind"]] += 1
        return (f"<b>{n}</b> trades · {len({r['session'] for r in rs})} session(s) · <b>{wins}</b> wins "
                f"({wins / n:.0%}) · total <b>{tot:+.2f}R</b> · avg {tot / n:+.3f}R · exits {dict(kinds)}")
    cards = "".join(f"<div class=card><h3>{html.escape(v)}</h3><p>{stats([r for r in rows if r['variant'] == v])}</p></div>"
                    for v in variants)
    uni = list(csv.DictReader(open(PATHS.universe))) if os.path.exists(PATHS.universe) else []
    days = ""
    for d in sorted({u["day"] for u in uni}, reverse=True)[:30]:
        chips = ""
        for u in (u for u in uni if u["day"] == d):
            lab = html.escape(f"{u['symbol']} · {u['sources'] or 'untagged'}")
            if u["qualified"] == "yes":
                chips += f"<span class=chip>{lab}</span>"
            else:
                chips += f"<span class='chip drop' title='{html.escape(u['reason'])}'>{lab}</span>"
        days += f"<p><b>{d}</b> {chips}</p>"
    head = "".join(f"<th>{c}</th>" for c in ledger.COLUMNS)
    body = ""
    for r in sorted(rows, key=lambda x: (x["session"], x["variant"], x["exit_time"]), reverse=True):
        rv = float(r["R"] or 0)
        cls = "pos" if rv > 0 else "neg" if rv < 0 else ""
        cells = "".join(f"<td class='{cls if c == 'R' else ''}'>{html.escape(str(r.get(c, '')))}</td>"
                        for c in ledger.COLUMNS)
        body += f"<tr data-v='{html.escape(r['variant'])}'>{cells}</tr>"
    opts = "".join(f"<option>{html.escape(v)}</option>" for v in variants)
    page = f"""<!doctype html><meta charset=utf-8><title>Trade ledger</title>
<style>
body{{font:14px system-ui,sans-serif;margin:24px;color:#1a1a1a}} h2{{font-size:16px;margin:22px 0 8px}}
.cards{{display:flex;gap:14px;flex-wrap:wrap}} .card{{border:1px solid #ddd;border-radius:8px;padding:10px 14px;background:#fafafa}}
.card h3{{margin:0 0 6px;font-size:14px}} .card p{{margin:0;font-size:13px}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{border-bottom:1px solid #eee;padding:5px 8px;text-align:right}}
th{{background:#f5f5f5;position:sticky;top:0}} .pos{{color:#0a7d33;font-weight:600}} .neg{{color:#c22;font-weight:600}}
.chip{{display:inline-block;border:1px solid #ccc;border-radius:10px;padding:1px 8px;margin:2px;font-size:12px}}
.chip.drop{{border-color:#c22;background:#fdeeee;color:#933;text-decoration:line-through}}
</style>
<h1>Trade ledger</h1><div class=cards>{cards}</div>
<h2>Universe — every ticker, every day; struck-through = dropped (hover for the reason)</h2>{days or '<p>none recorded</p>'}
<h2>All trades</h2>variant <select id=fv onchange=F()><option>all</option>{opts}</select>
<table><thead><tr>{head}</tr></thead><tbody id=tb>{body}</tbody></table>
<script>function F(){{const v=fv.value;for(const r of tb.rows)r.style.display=(v==='all'||r.dataset.v===v)?'':'none'}}</script>"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(page)
    return path


def main():
    ap = argparse.ArgumentParser(description="Record a completed session: cache, replay, real fills, report.")
    ap.add_argument("--strategy", default="macx")
    ap.add_argument("--day", default=None)
    ap.add_argument("--tf", default="1min")
    ap.add_argument("--report-only", action="store_true", help="just rebuild the HTML report")
    a = ap.parse_args()
    if not a.report_only:
        day = a.day or last_session()
        step(f"1/4 CACHE {day}", [PY, "cache_bars.py", day, "--tf", a.tf], tail=4)
        floor = EngineConfig().minstop
        for ms in (f"{floor:g}", "0"):
            step(f"2/4 REPLAY {day} (min stop {ms}%)",
                 [PY, "run_live.py", "--replay", "--day", day, "--tf", a.tf, "--strategy", a.strategy,
                  "--minstop", ms, "--client-id", "9", "--watchlist",
                  os.path.join(PATHS.watchlists, f"{day}.txt")], tail=3, must=True)
        step(f"3/4 REAL FILLS {day}", [PY, "real_ledger.py", day], tail=14)
    print("=" * 72 + f"\n4/4 REPORT -> {build_html()}")


if __name__ == "__main__":
    main()
