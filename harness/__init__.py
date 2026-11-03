"""intraday-trading-harness — the infrastructure, not the alpha.

    bars        the data atom + streaming bar builders + the disk cache
    strategy    the Setup contract every strategy implements
    engine      pre-armed brackets, broker-truth state, own-book accounting, watchdog, flatten
    parity      mechanical proof of the no-look-ahead contract
    universe    screening rules + a permanent record of every verdict
    watchlist   discovery by content, mid-session hot-add, point-in-time archive
    ledger      one row per trade per variant, replay and real side by side
    synthetic   deterministic sessions so everything runs without a broker
"""
