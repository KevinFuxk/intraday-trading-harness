"""Bars, watchlists, the universe screen and the ledgers — the data plumbing around the engine."""
import os
import time
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

from harness import ledger, universe, watchlist
from harness.bars import Bars, MinuteAggregator, ET
from harness.config import UniverseRules, EXAMPLE_STRICT_RULES
from tests.helpers import DAY


# ---------------------------------------------------------------- bars
def test_minute_aggregator_buckets_on_the_clock_and_closes_on_gaps():
    out = []
    agg = MinuteAggregator(2, lambda *b: out.append(b))
    t0 = datetime.combine(DAY, datetime.min.time()) + timedelta(hours=9, minutes=30)
    agg.add(t0, 10, 11, 9, 10.5, 100)
    agg.add(t0 + timedelta(minutes=1), 10.5, 12, 10, 11, 50)     # closes the 09:30 bucket
    assert out == [(t0, 10, 12, 9, 11, 150)]
    agg.add(t0 + timedelta(minutes=4), 11, 11.5, 10.8, 11.2, 70)  # 09:32 bucket skipped (gap)
    agg.add(t0 + timedelta(minutes=6), 11.2, 11.3, 11.1, 11.25, 10)  # a later bucket closes 09:34's
    assert out[-1][0] == t0 + timedelta(minutes=4) and out[-1][-1] == 70


def test_bars_prefix_is_exactly_what_live_had_seen():
    b = Bars("X", DAY, "1min")
    for i in range(5):
        b.append(DAY, i, i + 1, i - 1, i, 1)
    p = b.prefix(3)
    assert len(p) == 3 and p.c == [0, 1, 2] and len(b) == 5


# ---------------------------------------------------------------- watchlists
EXPORT = "###SOURCE ONE,NASDAQ:NVDA,NYSE:DE,###SOURCE TWO,NASDAQ:NVDA,AMEX:SPY,TVC:VIX"


def test_exports_are_recognized_by_content_not_by_filename(tmp_path):
    p = tmp_path / "anything at all.txt"
    p.write_text(EXPORT)
    assert watchlist.is_tradingview_export(str(p))
    p.write_text("A note to self about the market today, nothing more.")
    assert not watchlist.is_tradingview_export(str(p))
    p.write_text("NVDA\nAMD\n")
    assert not watchlist.is_tradingview_export(str(p)), "a hand-typed list is not an export"


def test_parser_reads_exports_verbatim_and_keeps_the_source_sections(tmp_path):
    p = tmp_path / "list.txt"
    p.write_text(EXPORT)
    syms, _ = watchlist.read(None, str(p))
    assert syms == ["NVDA", "DE", "SPY", "VIX"]
    assert watchlist.parse_sources(str(p))["NVDA"] == ["Source One", "Source Two"]
    assert watchlist.read("aapl, msft  nvda", str(p))[0] == ["AAPL", "MSFT", "NVDA"]


def test_hot_add_is_additive_and_only_from_todays_export(tmp_path):
    p = tmp_path / "list.txt"
    p.write_text("###LATE,NASDAQ:NVDA,NYSE:KO,NYSE:PG")
    today = datetime.now(ET).date()                  # the rule is judged in exchange time, not machine time
    assert watchlist.additions({"NVDA", "AAPL"}, str(p), today) == ["KO", "PG"]   # AAPL is never removed
    assert watchlist.additions({"NVDA", "KO", "PG"}, str(p), today) == []
    old = time.time() - 2 * 86400
    os.utime(p, (old, old))
    assert watchlist.additions({"NVDA"}, str(p), today) == [], "a stale export must inject nothing"


def test_the_archive_refuses_a_stale_export(tmp_path):
    p = tmp_path / "list.txt"
    p.write_text(EXPORT)
    dst = watchlist.archive(str(p), str(tmp_path / "archive"))
    assert dst and open(dst).read() == EXPORT
    old = time.time() - 2 * 86400
    os.utime(p, (old, old))
    assert watchlist.archive(str(p), str(tmp_path / "archive2")) is None


# ---------------------------------------------------------------- universe
def test_universe_rules_with_every_drop_explained():
    r = UniverseRules()
    assert universe.verdict(r, "NVDA", 36.7, NS(stockType="COMMON", industry="Technology", category="")) == ""
    assert "floor" in universe.verdict(r, "TINY", 3.10, NS(stockType="COMMON", industry="", category=""))
    assert "security type" in universe.verdict(r, "XLK", 100.0, NS(stockType="ETF", industry="", category=""))
    assert universe.verdict(r, "NVDA", None, None) == "", "unknown price/details is never a false drop"
    s = EXAMPLE_STRICT_RULES
    etf = NS(stockType="ETF", industry="", category="")
    assert universe.verdict(s, "DIA", 400.0, etf) == "", "whitelisted from the type rule"
    assert "floor" in universe.verdict(s, "IWM", 7.99, etf), "the whitelist waives the type rule ONLY"
    assert "sector" in universe.verdict(s, "UTIL", 60.0, NS(stockType="COMMON", industry="Utilities", category=""))


def test_universe_record_merges_by_day_and_symbol(tmp_path):
    path = str(tmp_path / "u.csv")
    universe.record([universe.row("2025-03-03", "AAA", ["One"], None, "")], path)
    universe.record([universe.row("2025-03-03", "AAA", ["One"], 4.0, "price $4.00 < $5 floor"),
                     universe.row("2025-03-04", "AAA", ["One"], 6.0, "")], path)
    rows = list(__import__("csv").DictReader(open(path)))
    assert [(r["day"], r["qualified"]) for r in rows] == [("2025-03-03", "no"), ("2025-03-04", "yes")]


# ---------------------------------------------------------------- ledgers
def test_the_ledger_rewrites_one_session_variant_and_leaves_the_rest(tmp_path):
    path = str(tmp_path / "ledger.csv")
    row = {"symbol": "A", "tf": "1min", "entry_time": "10:00", "exit_time": "10:30", "exit_kind": "TP", "R": "2.00"}
    ledger.write_variant("2025-03-03", "x:replay", [row, row], path)
    ledger.write_variant("2025-03-03", "x:armed", [row], path)
    ledger.write_variant("2025-03-03", "x:replay", [row], path)         # re-run: replaces, never appends
    rows = ledger.read(path)
    assert sorted(r["variant"] for r in rows) == ["x:armed", "x:replay"]
