"""The no-look-ahead contract (harness/strategy.py), checked mechanically by harness.parity —
on the demonstration strategy, and against deliberately broken strategies to prove the
checker actually catches each class of violation."""
import pytest

from harness.parity import check, assert_contract
from harness.strategy import Setup, Entry, FORMING, ENTERED
from harness.synthetic import session
from strategies.ma_cross import MACross


@pytest.mark.parametrize("seed", range(1, 13))
def test_demo_strategy_meets_the_contract(seed):
    rep = assert_contract(MACross(), session("DEMO", seed=seed))
    assert rep.batch_entries > 0, "the synthetic sessions must actually exercise entries"


def test_the_checker_catches_a_batch_detector_that_peeks_ahead():
    class Peeking(MACross):
        def detect(self, b):                         # books each entry one bar EARLY
            return [Entry(e.bar - 1, e.price) for e in super().detect(b)]
    rep = check(Peeking(), session("DEMO", seed=3))
    assert not rep.ok and rep.lagged and rep.matched == 0


def test_the_checker_catches_a_verdict_that_changes_after_it_was_final():
    class Flaky:
        name = "flaky"
        def scan(self, b, memo=None):
            n = len(b)
            if n < 10:
                return {}
            state = ENTERED if n == 10 else FORMING  # ENTERED, then un-enters
            return {5: Setup(5, state, 1.0, 0.5, 6, entry_bar=9 if state == ENTERED else None)}
        def detect(self, b):
            return [Entry(9, 1.0)]
    rep = check(Flaky(), session("DEMO", seed=1))
    assert rep.finality and not rep.ok


def test_the_checker_catches_a_setup_that_could_fill_before_it_was_known():
    class Early:
        name = "early"
        def scan(self, b, memo=None):
            return {7: Setup(7, FORMING, 1.0, 0.5, 7)} if len(b) > 7 else {}   # earliest == key
        def detect(self, b):
            return []
    rep = check(Early(), session("DEMO", seed=1))
    assert rep.timing == [(7, 7)] and not rep.ok


def test_scan_reads_only_the_prefix_it_is_given():
    b = session("DEMO", seed=5)
    s = MACross()
    full = s.scan(b)
    for n in (60, 150, 300):
        pre = s.scan(b.prefix(n))
        for key, st in pre.items():
            if st.state != FORMING:
                assert full[key] == st, f"a final verdict on bars[:{n}] changed with more bars"
