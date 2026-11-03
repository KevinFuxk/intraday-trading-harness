"""The broker's executions are the only truth: rebuilding trades from them, and the
leftover-position tool that keeps every session starting flat."""
from types import SimpleNamespace as NS

from flatten_leftovers import leftovers, working_closes
from real_ledger import reconstruct

REF_A = "alpha-XYZ-1min-2025-03-03-k40"
REF_B = "alpha-QRS-1min-2025-03-03-k12-r2"


def ex(time, sym, side, shares, price, ref=""):
    return {"time": time, "symbol": sym, "side": side, "shares": str(shares), "price": f"{price:.4f}", "ref": ref}


def test_trades_are_rebuilt_from_executions_with_partials_blended():
    executions = [
        ex("10:00:01", "XYZ", "BOT", 1500, 10.56, REF_A), ex("10:00:02", "XYZ", "BOT", 1000, 10.57, REF_A),
        ex("10:20:00", "XYZ", "SLD", 80, 10.79, REF_A),       # a partial take-profit
        ex("10:41:00", "XYZ", "SLD", 2420, 10.51, REF_A),     # the stop takes the rest
        ex("11:00:00", "QRS", "BOT", 200, 50.10, REF_B),
        ex("15:50:02", "QRS", "SLD", 200, 50.60, REF_B),      # closed by the engine's EOD order (ref'd)
    ]
    levels = {REF_A: {"trigger": 10.55, "stop": 10.52}, REF_B: {"trigger": 50.10, "stop": 49.85}}
    legs = {REF_A: {"TP", "SL"}, REF_B: {"EOD"}}
    trades, notes, churn = reconstruct(executions, levels, legs)
    by = {t["symbol"]: t for t in trades}
    a = by["XYZ"]
    entry = (1500 * 10.56 + 1000 * 10.57) / 2500
    exit_ = (80 * 10.79 + 2420 * 10.51) / 2500
    assert a["entry"] == f"{entry:.2f}" and a["exit"] == f"{exit_:.2f}" and a["exit_kind"] == "TP+STOP"
    assert a["R"] == f"{(exit_ - entry) / 0.03:.2f}", "R is measured against the bracket's own trigger-stop"
    assert by["QRS"]["exit_kind"] == "EOD" and by["QRS"]["R"] == f"{0.50 / 0.25:.2f}"
    assert notes == [] and churn == {}


def test_unattributed_closes_are_matched_fifo_and_anything_else_is_reported_not_dropped():
    executions = [
        ex("10:00:00", "XYZ", "BOT", 300, 20.00, REF_A),
        ex("15:50:00", "XYZ", "SLD", 300, 20.40),             # a close with no ref -> matched FIFO
        ex("09:30:01", "MNO", "BOT", 250, 95.00, "cleanup-2025-03-03"),   # another tool's ref
        ex("12:00:00", "JKL", "SLD", 100, 7.00),              # nothing explains this one
    ]
    trades, notes, churn = reconstruct(executions, {REF_A: {"trigger": 20.0, "stop": 19.8}}, {})
    assert len(trades) == 1 and trades[0]["exit_kind"] == "EOD" and trades[0]["R"] == "2.00"
    assert any("cleanup-2025-03-03" in n for n in notes), "a foreign ref is reported, never silently dropped"
    assert churn["MNO"][0] == 250 and churn["JKL"][0] == -100


def test_an_entry_with_no_exit_anywhere_is_flagged_open():
    trades, notes, _ = reconstruct([ex("15:15:00", "XYZ", "BOT", 247, 157.97, REF_A)], {}, {})
    assert trades == [] and any("OPEN" in n for n in notes)


# ---------------------------------------------------------------- leftover flatten
def test_leftover_rules_never_touch_a_position_traded_today():
    plan = {p[0]: p for p in leftovers({"AAA": 81, "BBB": -250, "CCC": 666, "DDD": 0}, {"CCC"})}
    assert "DDD" not in plan
    assert plan["AAA"][1:] == (81, "SELL", "")
    assert plan["BBB"][1:] == (-250, "BUY", ""), "a short is closed by buying"
    assert "traded TODAY" in plan["CCC"][3]
    assert "excluded" in {p[0]: p for p in leftovers({"AAA": 81}, set(), exclude={"AAA"})}["AAA"][3]


def test_working_closes_are_counted_so_a_second_run_places_nothing():
    """I-10: two runs 52 seconds apart rested two sells against one position."""
    def order(sym, action, qty, filled=0):
        return NS(contract=NS(symbol=sym), order=NS(action=action, totalQuantity=qty),
                  orderStatus=NS(status="PreSubmitted", filled=filled))
    orders = [order("AAA", "SELL", 81), order("AAA", "SELL", 81), order("AAA", "BUY", 10), order("ZZZ", "SELL", 5)]
    rest, live = working_closes(orders, "AAA", "SELL")
    assert rest == 162 and len(live) == 2, "both resting sells count — that total would reverse the position"
