"""
splits.py — Develop / Validate / Lockbox, enforced in code
===========================================================
A folder you can read is not a lockbox. This physically separates a dataset into three
time slices and enforces the access budget in code, with an append-only log of every look.

    Develop    tweak freely, unlimited access
    Validate   two looks, ever — each one counted and logged
    Lockbox    one look, at the very end, with a stated reason

    python splits.py --build example        # one time: copy the cache into the three slices
    python splits.py --status example       # how much of each budget is spent

    from splits import sessions, load_slice
    for sym, day in sessions("example", "develop"):
        rows = load_slice("example", "develop", sym, day)

Opening sealed data requires an explicit, logged reason — it cannot happen by accident:

    sessions("example", "lockbox", unlock="final test of the frozen rules")

Every access to a sealed slice is appended to data/splits/ACCESS_LOG.txt. That log is the
honest record of how many times the out-of-sample data was looked at — and the number a
reviewer should ask for before believing any out-of-sample result.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime

BASE = "data/splits"
LOG = f"{BASE}/ACCESS_LOG.txt"

DATASETS = {                                          # illustrative configuration
    "example": {
        "source": "cache/bars/5min",
        "symbols": ("AAPL", "MSFT"),
        "slices": {"develop": ("2015", "2020"), "validate": ("2021", "2022"), "lockbox": ("2023", "2025")},
        "note": "5-minute RTH bars, one JSON file per session",
    },
}
FREE = "develop"
LIMITS = {"validate": 2, "lockbox": 1}


def build(name: str) -> None:
    ds = DATASETS[name]
    made = {}
    for slice_name, (lo, hi) in ds["slices"].items():
        n = 0
        for sym in ds["symbols"]:
            src = f"{ds['source']}/{sym}"
            out = f"{BASE}/{name}/{slice_name}/{sym}"
            os.makedirs(out, exist_ok=True)
            for fn in sorted(os.listdir(src)) if os.path.isdir(src) else []:
                if fn.endswith(".json") and lo <= fn[:4] <= hi:
                    shutil.copy2(f"{src}/{fn}", f"{out}/{fn}")
                    n += 1
        made[slice_name] = n
        print(f"  {slice_name:9} {lo}-{hi}: {n:5} session files")
    manifest = {"dataset": name, "note": ds["note"], "built": datetime.now().isoformat(timespec="seconds"),
                "slices": {k: {"range": ds["slices"][k], "files": v} for k, v in made.items()},
                "rules": {"develop": "unlimited", "validate": "2 looks ever", "lockbox": "1 look, at the end"},
                "warning": ("Decisions made BEFORE this split saw the whole dataset. Validate and Lockbox are "
                            "clean only for rules frozen from the build date onward.")}
    os.makedirs(BASE, exist_ok=True)
    json.dump(manifest, open(f"{BASE}/{name}_manifest.json", "w"), indent=2)
    if not os.path.exists(LOG):
        open(LOG, "w").write("# every access to validate/lockbox is recorded here\n")
    print(f"\n  manifest -> {BASE}/{name}_manifest.json\n  access log -> {LOG}")


def _count(name: str, slice_name: str) -> int:
    if not os.path.exists(LOG):
        return 0
    tag = f"{name}/{slice_name}"
    return sum(1 for line in open(LOG) if line.startswith("ACCESS") and tag in line)


def _authorize(name: str, slice_name: str, unlock: str | None) -> None:
    if slice_name == FREE:
        return
    limit, used = LIMITS.get(slice_name), _count(name, slice_name)
    if unlock is None:
        raise PermissionError(f"'{slice_name}' is sealed ({used}/{limit} looks used). To open it, state a "
                              f"reason: sessions('{name}', '{slice_name}', unlock='why'). It will be logged.")
    if used >= limit:
        raise PermissionError(f"'{slice_name}' has been opened {used}/{limit} times — the budget is spent. "
                              f"Opening it again would make any result on it meaningless; collect new data.")
    os.makedirs(BASE, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"ACCESS {datetime.now().isoformat(timespec='seconds')}  {name}/{slice_name}  "
                f"(look {used + 1} of {limit})  reason: {unlock}\n")
    print(f"⚠️  opened {name}/{slice_name} — look {used + 1} of {limit}. Logged.", file=sys.stderr)


def sessions(name: str, slice_name: str, unlock: str | None = None) -> list[tuple[str, str]]:
    """[(symbol, 'YYYY-MM-DD'), ...] for one slice. Sealed slices need an unlock reason."""
    _authorize(name, slice_name, unlock)
    out = []
    for sym in DATASETS[name]["symbols"]:
        d = f"{BASE}/{name}/{slice_name}/{sym}"
        if os.path.isdir(d):
            out += [(sym, fn[:-5]) for fn in sorted(os.listdir(d)) if fn.endswith(".json")]
    return sorted(out, key=lambda x: (x[1], x[0]))


def load_slice(name: str, slice_name: str, symbol: str, day: str):
    """Raw rows for one session. Call sessions() first: that is where access is authorized."""
    p = f"{BASE}/{name}/{slice_name}/{symbol}/{day}.json"
    return json.load(open(p)) if os.path.exists(p) else None


def status(name: str) -> None:
    m = json.load(open(f"{BASE}/{name}_manifest.json"))
    print(f"{name} — {m['note']}   (built {m['built']})")
    for s, info in m["slices"].items():
        lo, hi = info["range"]
        lim = LIMITS.get(s)
        budget = "unlimited" if lim is None else f"{_count(name, s)}/{lim} looks used"
        print(f"  {s:9} {lo}-{hi}  {info['files']:5} files   {budget}")
    print(f"\n  ⚠️ {m['warning']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", metavar="DATASET")
    ap.add_argument("--status", metavar="DATASET")
    a = ap.parse_args()
    if a.build:
        build(a.build)
    elif a.status:
        status(a.status)
    else:
        ap.print_help()
