# Safety rails (LLM-independent)

The default values are in the generated table in the [README](../README.md).
Do not edit that table by hand.

The table is generated, not typed: `tools/readme_rails.py` parses `trader.py`
with `ast` and rewrites the block between the `RAILS` markers. Run `--check`
before committing (exit 1 on drift) and `--write` to regenerate. It reads the AST
rather than grepping the text because a regex cannot tell an active call from a
commented-out one, silently takes the first of two calls that disagree, and gives
up on any default that is not a plain string literal — every one of which can
publish a number the program never uses. Two live reads of the same variable with
different defaults are an error, not a coin flip. Every stale number this project
has published — a hardcoded `/20` on the GitHub profile, a hand-written model list
in the dashboard footer — was a value someone had copied. Copying is the bug;
deriving is the fix.

A BUY or SELL below the confidence floor is vetoed with the reason recorded, the
same way a Haiku veto or a contradicting sentiment read is. It has no env
override on purpose: measurement across this bot's own history found the
confidence score has no predictive value for outcome, so it is kept as a crude
floor rather than promoted to a ranking key.

Position sizing is a percentage of *portfolio value*, which says nothing about
settled cash. Until the **cash guard** was added, `MAX_POSITIONS x
POSITION_SIZE_PCT <= 100%` was the only thing keeping the bot off margin — and a
paper account is typically handed roughly 4 times buying power, so an over-budget order fills
silently on borrowed money rather than failing. `execute_trades()` now tracks
spendable cash across the run, trims an order to what cash covers, and records
`SKIPPED_CASH` once it is exhausted. Unfilled BUY orders left over from an earlier
run are charged against both the cash and the slot budget before either is spent
again, since Alpaca debits cash on fill rather than on submission. SELLs are
processed ahead of BUYs and their proceeds credited back, so a full book can still
rotate; a trim that would land below `MIN_TRADE_USD` is skipped rather than allowed
to spend a position slot on a stub.

The **trailing stop** arms only after a position's running peak gains
`TRAIL_ACTIVATE_PCT` above entry, then exits on a `TRAIL_STOP_PCT` pullback from
that peak — locking in gains on winners while leaving the hard floor to govern
names that never ran up. Peaks persist in `trade_logs/position_peaks.json` and are
sampled each run.
