"""
universe.py — which tickers may trade today, and a permanent record of why not
==============================================================================
Two hard-won rules:

  * The price rule is judged on the SESSION'S OPEN, before a single bar is walked. Judging
    a pre-open start on yesterday's close dropped names for the whole day on a price that
    was not the rule; judging a replay after walking the day left the dropped names'
    trades in the summary. A symbol started before the open is admitted provisionally
    and re-judged on its first bar (the engine calls back into the runner for that).
  * Every verdict is written down, tradable or not, with its reason. A universe you cannot
    reconstruct later is a backtest you cannot run later.
"""
from __future__ import annotations

import csv
import os

from .config import UniverseRules, PATHS

COLUMNS = ["day", "symbol", "sources", "n_sources", "open", "qualified", "reason"]


def verdict(rules: UniverseRules, sym: str, price: float | None, details) -> str:
    """'' if tradable, else the reason it is dropped. `details` is the broker's contract
    details (stockType / industry / category); None means unknown, never a false drop.
    The type whitelist waives ONLY the security-type rule — a whitelisted symbol still
    faces the price and sector rules."""
    st = (getattr(details, "stockType", "") or "").upper()
    if st and st not in rules.allowed_types and sym not in rules.type_whitelist:
        return f"not an allowed security type ({st})"
    if price is not None and price < rules.min_price:
        return f"price ${price:.2f} < ${rules.min_price:g} floor"
    ind = getattr(details, "industry", "") or ""
    cat = getattr(details, "category", "") or ""
    if ind in rules.excluded_industries or cat in rules.excluded_categories:
        return f"excluded sector ({ind} / {cat})"
    return ""


def row(day: str, sym: str, sources: list[str], price: float | None, why: str) -> dict:
    return {"day": day, "symbol": sym, "sources": "+".join(sources), "n_sources": len(sources),
            "open": f"{price:.2f}" if price is not None else "",
            "qualified": "no" if why else "yes", "reason": why}


def record(rows: list[dict], path: str = PATHS.universe) -> None:
    """Merge rows by (day, symbol): a later verdict for the same day replaces an earlier one
    (a pre-open provisional row is superseded by the verdict on the session's open)."""
    old = []
    if os.path.exists(path):
        keys = {(r["day"], r["symbol"]) for r in rows}
        old = [r for r in csv.DictReader(open(path)) if (r["day"], r["symbol"]) not in keys]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore", restval="")
        w.writeheader()
        for r in sorted(old + rows, key=lambda x: (x["day"], x["symbol"])):
            w.writerow(r)
