"""The engine's order-state machine, driven through a fake broker. Each test pins one
behaviour that a real incident taught (docs/INCIDENTS.md)."""
from types import SimpleNamespace as NS
from datetime import timedelta

from harness.engine import VERIFY_ATTEMPTS
from harness.strategy import Entry
from tests.helpers import (ScriptedStrategy, FakeBroker, Ev, flat_bars, forming, entered, make_trader,
                           fire_fill)


# ---------------------------------------------------------------- shadow lifecycle
def test_shadow_lifecycle_arms_at_earliest_fills_and_books_the_target():
    """PRE-ARM exactly when the next bar may fill, WOULD FILL on the crossing bar, and the
    labeler's conventions for the exit (checked from the bar after entry)."""
    s = ScriptedStrategy({20: forming(18, 100.51, 99.80, 20),
                          21: entered(18, 100.51, 99.80, 20, entry_bar=20)})
    t, _, said = make_trader(s, replay=True, tp=2.0)
    b = flat_bars(20)
    for i in range(20):
        t.on_closed_bar("TEST", "1min", b.ts[i], b.o[i], b.h[i], b.l[i], b.c[i], b.v[i])
    assert "TEST" in t.pending, "armed as soon as len(bars) reached earliest"
    nxt = b.ts[-1] + timedelta(minutes=1)
    t.on_closed_bar("TEST", "1min", nxt, 100.40, 100.60, 100.35, 100.55, 1000)       # crosses 100.51
    assert any("WOULD FILL" in l for l in said) and len(t.paper) == 1
    t.on_closed_bar("TEST", "1min", nxt + timedelta(minutes=1), 101.0, 102.0, 100.9, 101.9, 1000)
    assert len(t.closed) == 1 and t.closed[0]["kind"] == "TP"
    assert abs(t.closed[0]["r"] - 2.0) < 1e-9


def test_no_arming_after_the_eod_flatten_but_replay_still_arms():
    """I-7: aggregated bars can flush after the flatten; live must never arm off them.
    Replay walks symbols one after another, so it must stay exempt."""
    s = ScriptedStrategy({25: forming(22, 99.81, 99.20, 25)})
    live, _, _ = make_trader(s)
    live.eod_done = True
    live.reconcile("TEST", "1min", flat_bars(25))
    assert live.pending == {}, "live armed a bracket after the day's flatten"
    live.eod_done = False
    live.reconcile("TEST", "1min", flat_bars(25))
    assert "TEST" in live.pending
    rep, _, _ = make_trader(s, replay=True)
    rep.eod_done = True
    rep.reconcile("TEST", "1min", flat_bars(25))
    assert "TEST" in rep.pending


def test_min_stop_floor_and_position_cap_skip_with_a_reason():
    tight = ScriptedStrategy({25: forming(22, 100.00, 99.80, 25)})                    # 0.20% stop
    t, _, said = make_trader(tight, minstop=0.5)
    t.reconcile("TEST", "1min", flat_bars(25))
    assert t.pending == {} and any("floor" in l for l in said)
    ok = ScriptedStrategy({25: forming(22, 100.00, 99.00, 25)})
    t, _, said = make_trader(ok, max_positions=1, symbols=("TEST", "BUSY"))
    t.pending["BUSY"] = dict(tf="1min")                                               # the one slot is taken
    t.reconcile("TEST", "1min", flat_bars(25))
    assert "TEST" not in t.pending and any("at cap" in l for l in said)


# ---------------------------------------------------------------- broker truth (I-5)
def test_armed_entered_means_the_broker_filled_not_the_bar_crossed():
    script = {25: forming(22, 99.81, 99.20, 25),
              **{n: entered(22, 99.81, 99.20, 25, entry_bar=25) for n in (26, 27, 28)}}
    # (a) the bar crosses, the broker never fills: one bar of grace, then cancel + MISSED FILL
    t, br, said = make_trader(ScriptedStrategy(script), arm=True)
    t.reconcile("TEST", "1min", flat_bars(25))
    ref = t.pending["TEST"]["ref"]
    assert len(br.trades) == 3
    t.reconcile("TEST", "1min", flat_bars(26))
    assert "TEST" in t.pending and br.cancelled == [], "grace: a fill can lag the bar by seconds"
    t.reconcile("TEST", "1min", flat_bars(27))
    assert "TEST" not in t.pending and br.cancelled == [ref]
    assert any("MISSED FILL" in l for l in said)
    t.reconcile("TEST", "1min", flat_bars(28))
    assert len(br.trades) == 3, "no second bracket after a missed fill"
    # (b) the broker filled everything: settled, nothing cancelled
    t, br, _ = make_trader(ScriptedStrategy(script), arm=True)
    t.reconcile("TEST", "1min", flat_bars(25))
    br.trades[0].orderStatus.filled = t.pending["TEST"]["qty"]
    t.reconcile("TEST", "1min", flat_bars(26))
    assert "TEST" not in t.pending and br.cancelled == [] and 25 in t.entered_at[("TEST", "1min")]
    # (c) a partial entry: grace, then the REMAINDER is cancelled and the filled shares are kept
    t, br, said = make_trader(ScriptedStrategy(script), arm=True)
    t.reconcile("TEST", "1min", flat_bars(25))
    ref = t.pending["TEST"]["ref"]
    br.trades[0].orderStatus.filled = 5
    t.reconcile("TEST", "1min", flat_bars(26))
    assert "TEST" in t.pending and br.cancelled == []
    t.reconcile("TEST", "1min", flat_bars(27))
    assert "TEST" not in t.pending and br.cancelled == [ref] and any("PARTIAL" in l for l in said)


def test_a_quote_triggered_fill_before_the_bar_shows_the_cross_is_settled():
    script = {25: forming(22, 99.81, 99.20, 25), 26: forming(22, 99.81, 99.20, 25)}
    t, br, said = make_trader(ScriptedStrategy(script), arm=True)
    t.reconcile("TEST", "1min", flat_bars(25))
    br.trades[0].orderStatus.filled = t.pending["TEST"]["qty"]
    t.reconcile("TEST", "1min", flat_bars(26))
    assert "TEST" not in t.pending and any("quote-triggered" in l for l in said)


# ---------------------------------------------------------------- re-place (I-4)
def test_a_lower_stop_re_places_under_a_new_ref_and_a_fill_race_kills_the_successor():
    script = {25: forming(22, 99.81, 99.20, 25), 26: forming(22, 99.81, 99.10, 25)}
    t, br, said = make_trader(ScriptedStrategy(script), arm=True)
    t.reconcile("TEST", "1min", flat_bars(25))
    ref1 = t.pending["TEST"]["ref"]
    t.reconcile("TEST", "1min", flat_bars(26))
    p = t.pending["TEST"]
    assert br.cancelled == [ref1] and p["ref"] == ref1 + "-r2" and len(br.trades) == 6
    assert p["trades"]["TP"].order.ocaGroup == p["ref"], "the re-placed bracket is a NEW OCA group"
    assert abs(t.brackets[ref1]["stop"] - 99.20) < 1e-9, "the old ref keeps the levels it was sent with"
    assert abs(t.brackets[p["ref"]]["stop"] - 99.10) < 1e-9
    qty = t.brackets[ref1]["qty"]
    fire_fill(br.trades[0], qty, 99.81)              # the OLD parent filled inside the cancel round trip
    assert br.cancelled[-1] == p["ref"] and "TEST" not in t.pending
    assert t.open_real[ref1]["qty"] == qty and any("filled while being re-placed" in l for l in said)


def test_a_stop_that_moves_up_is_ignored_and_a_partly_filled_bracket_is_never_re_placed():
    up = ScriptedStrategy({25: forming(22, 99.81, 99.20, 25), 26: forming(22, 99.81, 99.40, 25)})
    t, br, _ = make_trader(up, arm=True)
    t.reconcile("TEST", "1min", flat_bars(25))
    t.reconcile("TEST", "1min", flat_bars(26))
    assert br.cancelled == [] and len(br.trades) == 3, "risk is never silently widened after sizing"
    down = ScriptedStrategy({25: forming(22, 99.81, 99.20, 25), 26: forming(22, 99.81, 99.10, 25)})
    t, br, _ = make_trader(down, arm=True)
    t.reconcile("TEST", "1min", flat_bars(25))
    br.trades[0].orderStatus.filled = 3
    t.reconcile("TEST", "1min", flat_bars(26))
    assert br.cancelled == [] and len(br.trades) == 3


# ---------------------------------------------------------------- own-book accounting (I-3)
def test_the_book_counts_shares_per_ref_and_closes_only_at_zero():
    t, br, said = make_trader(ScriptedStrategy({}), arm=True)
    ref = "test-XYZ-1min-2025-03-03-k40"
    t.brackets[ref] = dict(tf="1min", stop=10.52, target=10.79, trigger=10.55)
    c = NS(symbol="XYZ")

    def leg(kind, otype, level, sh, px):
        o = NS(orderRef=ref, orderType=otype, action="BUY" if kind == "ENTRY" else "SELL",
               lmtPrice=level if otype == "LMT" else 0, auxPrice=level if otype == "STP" else 0)
        t._fill_logger(kind)(NS(contract=c, order=o), NS(execution=NS(price=px, shares=sh), time="t"))
    leg("ENTRY", "STP", 10.55, 2500, 10.56)
    assert t.open_real[ref]["qty"] == 2500
    leg("TP", "LMT", 10.79, 80, 10.79)
    assert t.open_real[ref]["qty"] == 2420 and t.closed == [], "a partial exit keeps the rest on the book"
    assert any("still on the book" in l for l in said)
    leg("SL", "STP", 10.52, 2420, 10.51)
    assert ref not in t.open_real and len(t.closed) == 1
    blended = (80 * 10.79 + 2420 * 10.51) / 2500
    assert abs(t.closed[0]["exit"] - blended) < 1e-9


# ---------------------------------------------------------------- flatten (I-1, I-2, I-6, I-9)
def _book_with(t, ref, sym, qty):
    t.brackets[ref] = {}
    t.open_real[ref] = dict(sym=sym, tf="1min", entry=50.0, qty=qty, cost=50.0 * qty, out=0.0, out_qty=0,
                            stop=49.5, target=51.0, trigger=50.0, ts=None)


def test_flatten_closes_my_book_only_routed_on_my_contract_and_verifies():
    t, br, said = make_trader(ScriptedStrategy({}), arm=True, symbols=("XYZ",))
    mine = "test-XYZ-1min-2025-03-03-k7"
    _book_with(t, mine, "XYZ", 247)
    br.placeOrder(NS(symbol="XYZ"), NS(orderRef=mine, action="SELL", orderType="STP", parentId=0, totalQuantity=247))
    br.placeOrder(NS(symbol="OTHER"), NS(orderRef="other-OTHER-1", action="SELL", orderType="STP", parentId=0,
                                         totalQuantity=100))
    br.pos = {"XYZ": 347, "OTHER": 100}              # 100 XYZ belong to another strategy
    n_before = len(br.trades)
    t.flatten("EOD")
    assert br.cancelled == [mine], "only MY orders are cancelled — never a global cancel"
    closes = br.trades[n_before:]
    assert len(closes) == 1 and closes[0].order.action == "SELL" and closes[0].order.totalQuantity == 247
    assert closes[0].order.orderRef == mine, "the close carries the ref: attributable in the executions"
    assert closes[0].contract.exchange == "SMART", "routed on OUR qualified contract, not the listing venue"
    assert any("account holds +347" in l and "not mine" in l for l in said)
    closes[0].orderStatus.status = "Filled"
    assert t.verify_flatten() is True
    assert any("✅ close XYZ +247" in l for l in said) and not any("STILL OPEN" in l for l in said)
    n = len(br.trades)
    t.flatten("EOD")                                 # re-fired inside the window: nothing placed (I-2)
    assert len(br.trades) == n and any("already in flight" in l for l in said)


def test_verify_waits_for_a_working_close_before_shouting():
    """I-9: a market close on a thin book fills in pieces. 'Submitted' three seconds later is
    not a failure; it is re-checked. Only a close still not done on the final attempt shouts."""
    t, br, said = make_trader(ScriptedStrategy({}), arm=True, symbols=("XYZ",))
    _book_with(t, "test-XYZ-1min-2025-03-03-k7", "XYZ", 650)
    t.flatten("EOD")
    close = br.trades[-1]
    close.orderStatus.filled = 500                   # still Submitted, 500 of 650 done
    assert t.verify_flatten(1) is False
    assert any("still working" in l for l in said) and not any("STILL OPEN" in l for l in said)
    close.orderStatus.status, close.orderStatus.filled = "Filled", 650
    assert t.verify_flatten(2) is True
    assert any("✅ close XYZ +650" in l for l in said) and not any("STILL OPEN" in l for l in said)
    t2, br2, said2 = make_trader(ScriptedStrategy({}), arm=True, symbols=("XYZ",))
    _book_with(t2, "test-XYZ-1min-2025-03-03-k8", "XYZ", 100)
    t2.flatten("EOD")
    assert t2.verify_flatten(VERIFY_ATTEMPTS) is True
    assert any("STILL OPEN" in l and "XYZ +100" in l for l in said2), "a close never done must be shouted"


# ---------------------------------------------------------------- the watchdog (I-3, I-6)
def test_the_watchdog_guards_my_book_only_and_counts_only_live_exit_children():
    t, br, said = make_trader(ScriptedStrategy({}), arm=True, symbols=("XYZ", "RST", "OTHER"))
    ref, rest_ref = "test-XYZ-1min-2025-03-03-k40", "test-RST-1min-2025-03-03-k9"
    t.open_real = {ref: dict(sym="XYZ", qty=2420, stop=10.52)}
    t.brackets = {ref: {}, rest_ref: {}}
    t._parent_trades = {11: NS(orderStatus=NS(filled=2420)), 21: NS(orderStatus=NS(filled=0))}

    def resting(sym, r, otype, qty, pid):
        tr = br.placeOrder(NS(symbol=sym), NS(orderRef=r, action="SELL", orderType=otype, totalQuantity=qty,
                                              parentId=pid))
        return tr
    resting("XYZ", ref, "LMT", 2500, 11)            # an oversized take-profit, and NO stop at all
    resting("RST", rest_ref, "STP", 300, 21)        # children of a RESTING bracket — protect nothing yet
    resting("RST", rest_ref, "LMT", 300, 21)
    resting("OTHER", "other-OTHER-1", "STP", 100, 0)  # another strategy's — not ours to count or touch
    br.pos = {"XYZ": 2420, "OTHER": 100}
    n = len(br.trades)
    t.guard_brackets()
    placed = br.trades[n:]
    assert len(placed) == 1 and placed[0].order.orderType == "STP" and placed[0].order.totalQuantity == 2420
    assert placed[0].order.ocaGroup == ref and placed[0].order.orderRef == ref, "repair joins the OCA group"
    assert br.cancelled == [ref], "only the oversized take-profit is cancelled"
    assert sum("OTHER: account shows +100 sh, my book 0" in l for l in said) == 1
    t.guard_brackets()
    assert sum("OTHER: account shows" in l for l in said) == 1, "an account difference is reported once"
    t.open_real = {}                                 # the book went flat; the repair stop is now an orphan
    t.guard_brackets()
    assert br.cancelled[-1] == ref and any("orphan" in l for l in said)


def test_two_strategies_share_one_account_without_touching_each_other():
    """I-6, as an architecture property: refs are namespaced per strategy and the account
    view is read for alarms only."""
    broker = FakeBroker()
    a, _, said_a = make_trader(ScriptedStrategy({}, name="alpha"), arm=True, broker=broker, symbols=("XYZ",))
    b, _, said_b = make_trader(ScriptedStrategy({}, name="beta"), arm=True, broker=broker, symbols=("XYZ",))
    _book_with(a, "alpha-XYZ-1min-2025-03-03-k1", "XYZ", 100)
    _book_with(b, "beta-XYZ-1min-2025-03-03-k2", "XYZ", 300)
    broker.placeOrder(NS(symbol="XYZ"), NS(orderRef="beta-XYZ-1min-2025-03-03-k2", action="SELL",
                                           orderType="STP", parentId=0, totalQuantity=300))
    broker.pos = {"XYZ": 400}
    n = len(broker.trades)
    a.flatten("EOD")
    closes = broker.trades[n:]
    assert len(closes) == 1 and closes[0].order.totalQuantity == 100 and closes[0].order.orderRef.startswith("alpha-")
    assert broker.cancelled == [], "alpha must not cancel beta's resting stop"
    assert b.open_real["beta-XYZ-1min-2025-03-03-k2"]["qty"] == 300


# ---------------------------------------------------------------- the referee
def test_the_referee_marks_every_pre_armed_entry_not_just_the_last():
    s = ScriptedStrategy({}, entries=[Entry(30, 101.0), Entry(20, 100.5), Entry(25, 100.8)])
    t, _, said = make_trader(s, replay=True)
    t.store[("TEST", "1min")] = flat_bars(40)
    t.entered_at[("TEST", "1min")] = {20, 30}
    t.referee_once("TEST", "1min")
    lines = [l for l in said if "detector confirms" in l]
    assert [("pre-armed ✓" in l) for l in lines] == [True, False, True], lines
    assert "$100.50" in lines[0] and "$100.80" in lines[1], "reported in bar order"
