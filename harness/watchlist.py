"""
watchlist.py — the day's tickers: discovered by content, hot-added mid-session, archived
========================================================================================
The universe is a manually curated list exported from a charting platform (TradingView
format: one comma-separated line, `###SECTION` headers grouping tickers by the source that
surfaced them, `EXCHANGE:TICKER` tokens). Three properties matter more than parsing:

  * DISCOVERY BY CONTENT, NEVER BY FILENAME. Exports get renamed every day. Every
    filename rule eventually fails silently into a stale fallback list — so a file is
    recognized by what is inside it, and a fallback announces itself loudly.
  * ADDITIVE HOT-ADD. A list can grow after the open (some sources publish late). A newer
    export saved TODAY adds its new tickers to the running engine; nothing is ever removed
    mid-session (a removed symbol may hold a position), and a stale file injects nothing.
  * POINT-IN-TIME ARCHIVE. The curated list is the one input that cannot be reconstructed
    later. Each day's final export is archived verbatim, source tags included, so the
    forward test can become a backtest and every source can be evaluated on its own.
"""
from __future__ import annotations

import glob
import os
from datetime import datetime

from .bars import ET
from .config import PATHS

DEFAULT_SYMS = ["AAPL", "MSFT"]
SEARCH_DIR = os.path.expanduser("~/Downloads")
FALLBACK = "data/watchlist.txt"


def is_tradingview_export(path: str) -> bool:
    """A small text file whose fields are mostly ###SECTION headers or EXCHANGE:TICKER tokens."""
    try:
        if os.path.getsize(path) > 200_000:
            return False
        raw = open(path, errors="ignore").read(20_000)
    except OSError:
        return False
    fields = [f.strip() for f in raw.replace("\n", ",").split(",") if f.strip()]
    if not fields:
        return False
    tagged = sum(1 for f in fields if f.startswith("###")
                 or (":" in f and f.split(":")[0].isalpha() and f.split(":")[0].isupper()))
    return tagged >= max(2, len(fields) // 2)


def resolve(path: str = "auto", search_dir: str = SEARCH_DIR) -> str | None:
    """'auto' = the NEWEST export in search_dir (by modification time), else the fallback."""
    if path and path != "auto":
        return path if os.path.exists(path) else None
    cands = [c for c in glob.glob(os.path.join(search_dir, "*.txt")) if is_tradingview_export(c)]
    if cands:
        return max(cands, key=os.path.getmtime)
    return FALLBACK if os.path.exists(FALLBACK) else None


def _clean(raw: str) -> list[str]:
    """Raw export verbatim, or hand-typed tickers. Split on COMMAS first (section names can
    contain spaces), drop ###SECTION fields, strip EXCHANGE: prefixes, de-duplicate in order."""
    out, seen, junk = [], set(), []
    for line in raw.splitlines():
        s = line.strip()
        if not s or (s.startswith("#") and not s.startswith("###")):
            continue                                    # blank line / our own comment line
        for fld in s.split(","):
            fld = fld.strip()
            if not fld or fld.startswith("###"):
                continue                                # a section header
            if fld.startswith("#"):
                break                                   # trailing comment
            for tok in fld.split():
                t = tok.strip().upper().split(":")[-1]
                if not t:
                    continue
                if len(t) > 6 or not t.replace(".", "").replace("-", "").isalnum():
                    junk.append(tok)
                    continue
                if t not in seen:
                    seen.add(t)
                    out.append(t)
    if junk:
        print(f"⚠️ watchlist: ignored {len(junk)} non-ticker token(s): {', '.join(junk[:6])}")
    return out


def read(cli_symbols: str | None, path: str = "auto", search_dir: str = SEARCH_DIR) -> tuple[list[str], str]:
    """(tickers, where they came from). Priority: command line > export > fallback file >
    built-in list. Dependency-free: worst case it degrades to the built-in list."""
    if cli_symbols:
        return _clean(cli_symbols), "command line"
    resolved = resolve(path, search_dir)
    if resolved:
        syms = _clean(open(resolved, errors="ignore").read())
        if syms and resolved == FALLBACK:
            print("\n" + "!" * 78 + "\n!!  NO export found — using the STALE fallback " + FALLBACK +
                  "\n!!  If you exported today the file was not recognized: pass it with --watchlist\n"
                  + "!" * 78 + "\n")
        if syms:
            age_h = (datetime.now().timestamp() - os.path.getmtime(resolved)) / 3600
            stale = f"  ⚠️ {age_h / 24:.0f} days old — re-export?" if age_h > 20 else ""
            return syms, f"{resolved}{stale}"
    return list(DEFAULT_SYMS), "built-in default list"


def additions(known: set[str], path: str | None, today) -> list[str]:
    """New tickers to bring online mid-session: only from an export saved TODAY, only additions."""
    if not path or not os.path.exists(path):
        return []
    if datetime.fromtimestamp(os.path.getmtime(path), ET).date() != today:
        return []
    syms, _ = read(None, path)
    return [s for s in syms if s not in known]


def parse_sources(path: str) -> dict[str, list[str]]:
    """{ticker: [source sections it appeared under]}. A ticker surfaced by several
    independent sources is a hypothesis worth testing on its own."""
    tags: dict[str, list[str]] = {}
    section = "UNTAGGED"
    for fld in open(path).read().replace("\n", ",").split(","):
        fld = fld.strip()
        if not fld:
            continue
        if fld.startswith("###"):
            section = fld[3:].strip().title() or "Untagged"
            continue
        sym = fld.upper().split(":")[-1]
        if sym and len(sym) <= 6 and sym.replace(".", "").replace("-", "").isalnum():
            tags.setdefault(sym, [])
            if section not in tags[sym]:
                tags[sym].append(section)
    return tags


def archive(path: str | None, out_dir: str = PATHS.watchlists) -> str | None:
    """Copy today's export to <out_dir>/YYYY-MM-DD.txt. Refuses a stale export: a file saved
    on an earlier day must never enter the point-in-time record. Idempotent."""
    if not path or not os.path.exists(path):
        return None
    if datetime.fromtimestamp(os.path.getmtime(path), ET).date() != datetime.now(ET).date():
        print("  📚 watchlist NOT archived — the export is from an earlier day")
        return None
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, f"{datetime.now(ET):%Y-%m-%d}.txt")
    content = open(path, errors="ignore").read()
    if os.path.exists(dst) and open(dst, errors="ignore").read() == content:
        return dst
    with open(dst, "w") as f:
        f.write(content)
    print(f"  📚 watchlist archived -> {dst}")
    return dst
