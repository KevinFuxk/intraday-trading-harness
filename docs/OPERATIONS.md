# Operations

The daily routine for a forward test. It is three commands, because a step that is easy to forget
eventually gets forgotten ([I-14](INCIDENTS.md#i-14)).

> **One login per account while an engine runs.** Signing in from another device ends the API session
> ([I-13](INCIDENTS.md#i-13)).

## Before the open

```bash
python flatten_leftovers.py            # dry run: what did a previous session leave behind?
python flatten_leftovers.py --execute  # close it; queues for the opening print if pre-market
```

Then save the day's watchlist export to the watch folder (default `~/Downloads`). The filename does
not matter; exports are recognized by content.

## The session

```bash
python run_live.py --arm --base 25000
```

`--base` sizes a virtual account on top of whatever the paper account holds. Leave it running. New
symbols added to the export during the session are picked up within 30 seconds; nothing is ever
removed mid-session. **Ctrl-C** flattens the book and verifies it before exiting.

The terminal shows a status board. The full narration goes to `logs/<strategy>_<date>.log`:

| Line | Meaning |
|---|---|
| `🛡️ PRE-ARMED` | a bracket is resting at the broker |
| `🔧` / `🔁` | the stop moved down; the bracket was cancelled and re-placed under a new ref |
| `🗑️ CANCEL` | the setup died; its bracket was cancelled |
| `💰 FILL` | a fill, with slippage against the intended level |
| `↕️` | a partial exit; the remainder stays on the book under a stop |
| `❌ MISSED FILL` | the bar crossed, the broker filled nothing; cancelled and recorded |
| `🚨 guard` | the watchdog repaired stop coverage or cancelled an oversized exit |
| `ℹ️` | the account differs from my book: another strategy's or a previous run's shares |
| `📋` | the referee: a batch-detector entry, and whether the engine pre-armed it |
| `⛔ FLATTEN` then `✅ close` | the end-of-day flatten and its broker confirmation |
| `🚨 STILL OPEN` | a close the broker has not confirmed after every retry. **Act on it** |

## After the close

```bash
python evening_ritual.py
```

It caches the session's bars, replays the session twice (with and without the minimum-stop gate),
rebuilds the real trades from the broker's executions, and regenerates `data/ledger.html`. **Run it
the same evening**: the broker serves executions for the current day only.

## Completeness audit

For every trading day, these should exist:

| Artifact | Written by |
|---|---|
| `data/watchlists/<date>.txt` | the session |
| `data/universe_log.csv` rows for the date | the session |
| `logs/<strategy>_<date>.log` | the session |
| `cache/bars/<tf>/*/<date>.json` | the evening ritual |
| `data/executions/<date>.csv` | the evening ritual |
| ledger rows, every variant | the evening ritual |

A day missing the first three was never traded. A day missing the last three was traded but never
recorded, and its executions can no longer be recovered from the broker.

## Client ids

One per concurrent connection. The defaults are chosen not to collide:

| Process | Client id |
|---|---|
| `run_live.py` (live) | 8 |
| `run_live.py --replay` | 9 |
| `cache_bars.py` | 52 |
| `real_ledger.py` | 54 |
| `flatten_leftovers.py` | 70 |
