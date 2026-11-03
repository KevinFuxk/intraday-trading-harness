# Research discipline

The harness exists to answer one question honestly: does a strategy that looked good in research
survive contact with a real order book? Most of the ways that question gets answered *dishonestly*
are invisible from the inside, so each guard below is built into the tooling rather than left to good
intentions.

## 1. Freeze the evaluation before looking at results

The exit model (fixed R-multiple targets across a grid, full-path labeling, stop before target when a
bar touches both), the cost assumptions, and the population rules are fixed **before** a strategy
variant is evaluated. A result is never re-sliced, re-filtered or re-exited after it has been seen.
An evaluation that changes after the fact is a search, and searches find something every time.

## 2. Out-of-sample data is a budget, enforced in code

[`splits.py`](../splits.py) cuts a dataset into *develop*, *validate* and *lockbox* time slices and
enforces the access rules in code: unlimited, two looks, one look. Opening a sealed slice requires a
stated reason and is appended to an access log. The log answers the question a reviewer should always
ask: *how many times was the out-of-sample data looked at before this result was reported?*

The manifest also states the caveat plainly: any decision made before the split was built saw the
whole dataset, so the sealed slices are clean only for rules frozen after that date.

## 3. Compare against a null, built correctly

A strategy's average R means little alone; it means something next to a null benchmark: random
entries on the same sessions, with the same stop distances, targets, holding rules and costs, drawn
repeatedly. The non-obvious trap is the matching window. A null whose entries are drawn from the
window *before* each real entry silently embeds the future breakout the strategy is waiting for, and
it looks far better than any achievable entry. Draws must come from the same session without
borrowing information from the real signal's timing.

## 4. Prove there is no look-ahead

A batch backtest sees the whole session; a live process sees closed bars only. The strategy contract
([strategy.py](../harness/strategy.py)) makes the difference testable: a streaming `scan()` and an
independent batch `detect()`, checked by [`parity.py`](../harness/parity.py) bar by bar. Every batch
entry must reappear on the *same* bar in the stream. A lag means the batch implementation peeked; a
final verdict that changes means the stream peeked. The private strategies were held to the same check
on real sessions, including an equivalence test that pins their memoized fast path to the full scan.

## 5. Measure the fill fantasy, every day

Replay fills at the trigger; the broker does not. The replay and the real fills land in the same
ledger as different variants of the same session, so the gap between what the rules said and what the
market gave is one filter away every evening. Slippage is logged per fill against the intended level,
and bars that crossed a trigger without a fill are recorded as `MISSED`. That measured gap is what a
minimum-stop floor should be calibrated against: a stop narrower than the execution noise turns a
planned 1R loss into a multiple of it before the market moves at all.

## 6. Forward testing is the only clean out-of-sample

Historical data, however carefully split, was chosen with hindsight. A forward test on a paper account
is the one dataset nobody could have looked at in advance. Its verdict is pre-registered: a minimum
trade count per strategy, net R per trade, and a t-statistic against the same null machinery. And
because the universe is curated by hand, the forward test is also the only way its history will ever
exist, which is why every input is archived on the day it happens.
