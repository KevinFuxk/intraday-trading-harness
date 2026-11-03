# Incidents

Every rule in the engine that looks paranoid exists because the simpler version failed while
paper trading. Each entry below is what happened, why, what changed, and the test that keeps it
from happening again. Symbols, dates and P&L are omitted; the engineering is not.

| | Incident | Class |
|---|---|---|
| [I-1](#i-1) | The listing exchange is not an order route | broker semantics |
| [I-2](#i-2) | A blocking sleep inside the event loop | concurrency |
| [I-3](#i-3) | OCA type 1 cancels the sibling on a partial fill | broker semantics |
| [I-4](#i-4) | OCA children cannot be revised | broker semantics |
| [I-5](#i-5) | The bar said "entered"; the broker said no | state truth |
| [I-6](#i-6) | The account view is not my book | multi-strategy |
| [I-7](#i-7) | Arming after the flatten | lifecycle |
| [I-8](#i-8) | The universe judged on the wrong price | data |
| [I-9](#i-9) | A working close reported as a failure | alerting |
| [I-10](#i-10) | Two cleanup orders against one position | idempotency |
| [I-11](#i-11) | A phantom exit in a second engine | state truth |
| [I-12](#i-12) | A stale watchlist, silently | data |
| [I-13](#i-13) | A second login ended the API session | operations |
| [I-14](#i-14) | Sessions that were never recorded | operations |
| [I-15](#i-15) | Tests writing into the production log | testing |

---

<a id="i-1"></a>
### I-1 · The listing exchange is not an order route

**What happened.** An end-of-day close was placed on the contract object the positions report
returns. Its exchange field is the *listing* venue, which is not a routable destination. The broker
rejected the order without raising anything the code was watching. The protective stop had already
been cancelled as the first step of the flatten, so the position was carried overnight with no stop.

**Fix.** Every close goes out on the engine's own qualified, SMART-routed contract. A flatten is not
considered done until the broker's order status is read back; anything still open is shouted with
an explicit instruction to close it by hand.

**Guard.** `test_flatten_closes_my_book_only_routed_on_my_contract_and_verifies`

---

<a id="i-2"></a>
### I-2 · A blocking sleep inside the event loop

**What happened.** The flatten's confirmation step slept for three seconds and then checked
positions. The flatten was triggered from inside a bar-stream callback, which runs on the broker
client's event loop, where a blocking sleep raises `event loop is already running`. The event
library swallowed the exception. So the confirmation never ran, and neither did the line after the
flatten that marked the day as flattened.

Every subsequent bar fired the flatten again: dozens of times in thirty seconds. Position reports lag
fills by a few hundred milliseconds, so each re-fire sold shares that had already been sold. The book
went short, bought back, sold again, and finished the day holding a short position overnight.

**Fix.** Four independent layers, because any one alone would have been enough to stop the storm:
the end-of-day flag is set *before* the flatten runs; a second flatten inside a 15-second window
places nothing; a per-order-ref in-flight guard refuses a second close while one is working; and
confirmations are scheduled on the running loop with `call_later`, never slept for.

**Guard.** The idempotency assertions in `test_flatten_closes_my_book_only_…`; `Trader._defer`.

---

<a id="i-3"></a>
### I-3 · OCA type 1 cancels the sibling on a partial fill

**What happened.** Exits were a one-cancels-all pair: a take-profit limit and a stop. A take-profit
filled a few percent of the position. With OCA type 1 *any* fill of one leg cancels the other, so the
stop for the whole position was cancelled. The remaining shares rode unprotected to the close.

**Fix.** OCA type 2, which reduces the sibling proportionally on a partial fill. The book counts
shares per order ref (entry fills add, exit fills subtract) and books a trade closed only at zero.
A watchdog on the main thread every 30 seconds restores stop coverage for every share on the book and
cancels any exit order larger than the remaining position.

**Guard.** `test_the_book_counts_shares_per_ref_and_closes_only_at_zero`,
`test_the_watchdog_guards_my_book_only_and_counts_only_live_exit_children`

---

<a id="i-4"></a>
### I-4 · OCA children cannot be revised

**What happened.** When a setup's stop moved, the engine modified the stop leg in place. The broker
does not allow revisions to members of an OCA group (error 10326). The modification was rejected
while the engine's log said it had been applied.

**Fix.** Cancel the whole unfilled bracket and re-place it under a **new order ref**, which is a new
OCA group. That opens a race: the old parent can fill during the cancel's round trip. So the old ref
remembers its successor, and its fill handler cancels the successor, so a position can never be
doubled. A bracket with any fill at all is never re-placed. A stop that moves *up* is ignored: the
risk a bracket was sized for is never widened after the fact.

**Guard.** `test_a_lower_stop_re_places_under_a_new_ref_and_a_fill_race_kills_the_successor`,
`test_a_stop_that_moves_up_is_ignored_and_a_partly_filled_bracket_is_never_re_placed`

---

<a id="i-5"></a>
### I-5 · The bar said "entered"; the broker said no

**What happened.** When a bar's high crossed the trigger, the engine treated the setup as filled and
freed the symbol's slot. But a stop order triggers on *quotes*, not on a single print: the bar
crossed, the order never triggered, and it kept resting. The freed slot let a second setup arm on the
same symbol. Both later filled, which doubled the position.

**Fix.** In armed mode "entered" means exactly one thing: the parent order's filled quantity. A bar
that crosses with no fill gets one bar of grace, after which the bracket is cancelled and recorded as
a `MISSED FILL`, which is itself a cost-model datapoint. A partial entry cancels the remainder and
keeps the filled shares under their exits. A fill that arrives *before* the bar shows the cross is
settled on the spot.

**Guard.** `test_armed_entered_means_the_broker_filled_not_the_bar_crossed`,
`test_a_quote_triggered_fill_before_the_bar_shows_the_cross_is_settled`

---

<a id="i-6"></a>
### I-6 · The account view is not my book

**What happened.** Two strategies traded one account. Reading positions from the account made each
strategy count the other's shares as its own: it refused its own setups on that symbol, used up
position slots, and its end-of-day flatten sold the other strategy's shares. Its global cancel
killed the other strategy's resting orders.

**Fix.** Own-book accounting. Each engine namespaces every order ref with its strategy name and acts
only on its own refs and its own fills. The account view is still read, but for alarms only: a
difference between account and book is reported once and never acted on.

**Guard.** `test_two_strategies_share_one_account_without_touching_each_other`

---

<a id="i-7"></a>
### I-7 · Arming after the flatten

**What happened.** Higher-timeframe bars are aggregated locally from one base stream, and a bucket is
emitted when it closes, which can be *after* the day's flatten. A setup armed from such a bar would
leave a fresh day order resting with nothing scheduled to flatten it. A fill there would be held
overnight.

**Fix.** Live mode never arms after the day's flatten. Replay is deliberately exempt: it walks
symbols one after another, so a later symbol's whole day arrives after an earlier symbol's EOD, and
the same gate silently broke replay the first time it was tried.

**Guard.** `test_no_arming_after_the_eod_flatten_but_replay_still_arms`

---

<a id="i-8"></a>
### I-8 · The universe judged on the wrong price

**What happened.** Started before the open, the screen applied the price rule to the previous close
and dropped symbols for the whole session on a price that was not the rule. In replay, the screen ran
*after* the session had been walked, so a dropped symbol's trades still appeared in the summary.

**Fix.** The screen runs on the session's open, before a single bar is walked. A symbol started
before the open is admitted provisionally and judged on its first bar, through a callback from the
engine to the runner.

**Guard.** `test_universe_rules_with_every_drop_explained`; `Trader.on_closed_bar` → `universe_hook`.

---

<a id="i-9"></a>
### I-9 · A working close reported as a failure

**What happened.** The post-flatten check ran three seconds after the closes were sent. On a thin
book a market order fills in pieces, so a close that was still working (and partly filled) was
reported as `STILL OPEN — CLOSE MANUALLY NOW`. It filled a second later. It happened twice in four
sessions. An alarm that cries wolf teaches people to ignore it.

**Fix.** A working close is re-checked, up to three times, before it is called a failure, and the
final verdict states filled versus ordered shares.

**Guard.** `test_verify_waits_for_a_working_close_before_shouting`

---

<a id="i-10"></a>
### I-10 · Two cleanup orders against one position

**What happened.** The leftover-position tool was run twice within a minute. Each run placed a market
sell for the full position, so two orders rested against one position. At the open both would have
filled and reversed it into a short. The second run could not see the first run's order, because the
broker client's open-orders view is scoped to the client id of the current session.

**Fix.** Count the *account's* working closes first, using `reqAllOpenOrders`, which sees every
client's orders. Then place only the shortfall, report an oversized total, and trim it on request.

**Guard.** `test_working_closes_are_counted_so_a_second_run_places_nothing`

---

<a id="i-11"></a>
### I-11 · A phantom exit in a second engine

**What happened.** A separate strategy's process decided its exits from bar logic and recorded a
stop-out that never reached the broker. Its records said flat. The account held the position for
several sessions.

**Fix.** Make I-5's rule universal: the broker's executions are the only truth about exits as well as
entries. Two independent backstops now exist. `flatten_leftovers.py` closes anything a previous
session left behind every morning, and `real_ledger.py` reports any execution that no strategy
bracket explains.

**Guard.** `test_unattributed_closes_are_matched_fifo_and_anything_else_is_reported_not_dropped`,
`test_an_entry_with_no_exit_anywhere_is_flagged_open`

---

<a id="i-12"></a>
### I-12 · A stale watchlist, silently

**What happened.** The day's watchlist was found by filename pattern. The export was renamed, nothing
matched, and the engine fell back to an old list and traded it. That happened on two sessions before
anyone noticed.

**Fix.** Exports are recognized by their *content*. A fallback prints a banner that cannot be missed.
A stale export is never archived as the day's universe.

**Guard.** `test_exports_are_recognized_by_content_not_by_filename`,
`test_the_archive_refuses_a_stale_export`

---

<a id="i-13"></a>
### I-13 · A second login ended the API session

**What happened.** Signing in to the broker from a phone while the engine was running ended the
engine's session ("connected from a different IP address"). Startup kept failing for half an hour, and
the opening of that session was never traded or recorded.

**Lesson.** This one is operational, not code. One login per account while an engine runs. It is the
first line of the runbook.

---

<a id="i-14"></a>
### I-14 · Sessions that were never recorded

**What happened.** In one week the engine was not started on two of five trading days. With a curated
universe, a session that is not captured on the day is gone: no archived watchlist, no cached bars, no
universe verdicts. There is nothing to reconstruct it from.

**Fix.** A three-command daily routine ([OPERATIONS.md](OPERATIONS.md)) and a completeness audit with
one row per trading day and one column per artifact, so a missing day is visible the next morning
rather than months later.

---

<a id="i-15"></a>
### I-15 · Tests writing into the production log

**What happened.** A unit test's engine wrote its narration into the real session log, interleaving
fabricated lines with a live day's record.

**Fix.** Every file the engine writes (log, fills, brackets) is a constructor argument. Tests pass
`os.devnull` or `None`.
