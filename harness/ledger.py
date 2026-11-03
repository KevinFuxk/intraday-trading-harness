"""
ledger.py — one row per closed trade, per variant, per session
==============================================================
Variants sit side by side in ONE file so the gap between them is always one filter away:

    replay variants   what the rules SAID the session was worth (shadow fills at the level)
    armed             what the broker actually GAVE (real fills, from its executions)

Writes are idempotent: re-recording a (session, variant) replaces exactly those rows.
"""
from __future__ import annotations

import csv
import os

from .config import PATHS

COLUMNS = ["session", "variant", "symbol", "tf", "entry_time", "entry", "trigger", "stop",
           "stop_pct", "exit_time", "exit_kind", "exit", "R", "peak_R"]


def from_closed(closed: list[dict]) -> list[dict]:
    """The engine's closed-trade dicts -> ledger rows (without session / variant)."""
    out = []
    for c in closed:
        trig, stop = c.get("trigger"), c.get("stop")
        out.append({
            "symbol": c["sym"], "tf": c["tf"],
            "entry_time": f"{c['entry_ts']:%H:%M}" if c.get("entry_ts") else "",
            "entry": f"{c['entry']:.2f}" if c.get("entry") is not None else "",
            "trigger": f"{trig:.2f}" if trig else "", "stop": f"{stop:.2f}" if stop else "",
            "stop_pct": f"{(trig - stop) / trig * 100:.2f}" if trig and stop else "",
            "exit_time": f"{c['ts']:%H:%M}", "exit_kind": c["kind"],
            "exit": f"{c['exit']:.2f}", "R": f"{c['r']:.2f}",
            "peak_R": f"{c['mfe']:.2f}" if c.get("mfe") is not None else ""})
    return out


def write_variant(session: str, variant: str, rows: list[dict], path: str = PATHS.ledger) -> None:
    old = []
    if os.path.exists(path):
        old = [r for r in csv.DictReader(open(path))
               if not (r.get("session") == session and r.get("variant") == variant)]
    for r in rows:
        old.append({"session": session, "variant": variant, **r})
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore", restval="")
        w.writeheader()
        for r in sorted(old, key=lambda x: (x["session"], x["variant"], x["exit_time"])):
            w.writerow(r)


def read(path: str = PATHS.ledger) -> list[dict]:
    return list(csv.DictReader(open(path))) if os.path.exists(path) else []
