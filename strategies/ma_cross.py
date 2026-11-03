"""
ma_cross.py — DEMONSTRATION ONLY: a textbook moving-average cross
=================================================================
This exists so the harness can run end to end on any machine. It is deliberately the
most generic signal in the literature, it claims no edge, and it has nothing in common
with the strategies the harness was built to run (those are private and not published).

    setup born   on the CLOSE of bar k where the fast SMA crosses above the slow SMA
    trigger      bar k's high + $0.01        (a resting buy-stop)
    stop         lowest low of the last `stop_lookback` bars, fixed at birth
    earliest     k + 1                       (the order rests from the next bar)
    ENTERED      the first later bar whose high reaches the trigger
    DEAD         a bar trades below the stop first, or `expiry` bars pass untriggered

It also shows how a strategy meets the contract in harness/strategy.py:

  * scan() reads only bars[:n] and walks forward from each cross, so it is causal;
  * detect() is a SEPARATE batch implementation (running sums over the full session)
    that the engine uses as its referee;
  * the moving-average comparison is done in INTEGER CENTS, so the two implementations
    can never disagree through floating-point rounding at an exact tie.
"""
from __future__ import annotations

from harness.bars import Bars
from harness.strategy import Setup, Entry, FORMING, ENTERED, DEAD


def _cents(prices) -> list[int]:
    return [int(round(p * 100)) for p in prices]


class MACross:
    name = "macx"

    def __init__(self, fast: int = 5, slow: int = 20, stop_lookback: int = 5, expiry: int = 10):
        assert 1 <= fast < slow and stop_lookback >= 1 and expiry >= 1
        self.fast, self.slow, self.lb, self.expiry = fast, slow, stop_lookback, expiry

    # ---- live: what does the session look like after n closed bars? --------------------
    def scan(self, b: Bars, memo: dict | None = None) -> dict[int, Setup]:
        n = len(b)
        cents = _cents(b.c)
        out: dict[int, Setup] = {}
        for k in range(self.slow, n):                # bar k-1 needs a full slow window too
            if not self._crossed_up(cents, k):
                continue
            trigger = round(b.h[k] + 0.01, 2)
            stop = round(min(b.l[max(0, k - self.lb + 1): k + 1]), 2)
            state, entry_bar = FORMING, None
            for j in range(k + 1, n):
                if b.h[j] >= trigger:
                    state, entry_bar = ENTERED, j
                    break
                if b.l[j] < stop or j - k >= self.expiry:
                    state = DEAD
                    break
            out[k] = Setup(key=k, state=state, trigger=trigger, stop=stop, earliest=k + 1,
                           entry_bar=entry_bar, label="ma-cross")
        return out

    def _crossed_up(self, cents: list[int], k: int) -> bool:
        f, s = self.fast, self.slow
        fast_now, slow_now = sum(cents[k - f + 1:k + 1]), sum(cents[k - s + 1:k + 1])
        fast_prev, slow_prev = sum(cents[k - f:k]), sum(cents[k - s:k])
        # mean_f > mean_s  <=>  sum_f * s > sum_s * f   (exact, in integers)
        return fast_now * s > slow_now * f and fast_prev * s <= slow_prev * f

    # ---- the referee: an independent batch pass over a complete session ---------------
    def detect(self, b: Bars) -> list[Entry]:
        cents = _cents(b.c)
        run = [0]
        for x in cents:
            run.append(run[-1] + x)                  # run[i] = sum(cents[:i])
        window = lambda i, w: run[i + 1] - run[i + 1 - w]
        above = [None] * len(b)
        for i in range(self.slow - 1, len(b)):
            above[i] = window(i, self.fast) * self.slow > window(i, self.slow) * self.fast
        entries = []
        for k in range(self.slow, len(b)):
            if not (above[k] and above[k - 1] is False):
                continue
            trigger = round(b.h[k] + 0.01, 2)
            stop = round(min(b.l[max(0, k - self.lb + 1): k + 1]), 2)
            for j in range(k + 1, min(len(b), k + self.expiry + 1)):
                if b.h[j] >= trigger:
                    entries.append(Entry(bar=j, price=max(trigger, b.o[j])))
                    break
                if b.l[j] < stop:
                    break
        return entries
