# Results and retirement

Alpaca paper account, $100,000 start, 2026-04-14 to the 2026-10-09 close (120
trading days). Final equity, cash and the 24 open positions were verified
against Alpaca on 2026-10-10.

| Metric | Result |
|--------|--------|
| Final equity | $107,064.60 (**+7.06%**) |
| SPY, same window (adj. close) | **+12.68%** |
| Bot minus SPY | **-5.62 pp** |
| Regression vs SPY (daily, n=124) | beta 0.417, alpha +3.9%/yr, **t = 0.35** |
| Alpha 95% CI | [-18.3%, +26.1%] per year (residual vol 7.80%/yr) |
| Sharpe (rf=0) | bot 1.55, SPY 2.08 |
| Max drawdown (daily closes) | bot -3.64%, SPY -4.49% |
| Trades | 290 buys, 310 sells; 41.0% winning sells; profit factor 1.20 |
| P&L by exit type | trailing stops +$15,857; hard stops -$12,610; AI exits -$1,345; AI trims -$117; signal sells +$2,656 |
| Live trading needed to show alpha != 0 at t=2 | ~16 years |

The alpha estimate is statistically indistinguishable from zero: the confidence
interval spans -18.3% to +26.1% a year.

Notes on the data:

- Final state: cash $8,494.42; 24 positions with a market value of $98,570.18,
  frozen at retirement. It is a paper account, so they keep marking to market; the
  numbers above are the official final snapshot.
- Gaps: 2026-06-30 to 2026-07-13 was an outage on the old Pi (7 trading days,
  positions held), and there were no runs on 2026-08-26.
- 2026-10-09 ran degraded (agy quota out, Gemini flash-lite fallback). Excluding
  it the result is +6.27% vs SPY +12.01%, so the headline does not change.
- LLM volume: 6,939 analyst calls (67,295 ticker analyses) and 27,489 LLM exit
  reviews. Metered Claude spend was $0.49 logged (412 Haiku calls, 2026-08-17 to
  2026-10-09), plus roughly $34 estimated from before logging began.

## Why it was retired

The owner's decision, 2026-10-10:

1. **The research question is unanswerable on any useful timescale.** Proving
   alpha would take about 16 years of live running.
2. **It underperformed buy-and-hold SPY by 5.6 pp** with lower beta; risk-adjusted
   it also trailed (Sharpe 1.55 vs 2.08).
3. **The LLM path was a maintenance cost with no research return.** It drained the
   owner's main-account Gemini quota (~320 calls/day), and the provider chain kept
   degrading (Hugging Face credits, agy quota, DeepSeek-proxy ban risk).

## What it taught

- A fallback that shares its primary's billing rail is not a fallback.
- Alert on degraded LLM output, not only on outright failure.
- Timezone and cron traps are real.
- Run a power analysis before "let it run to find out".
- Four mechanisms biased the bot toward inaction (HOLD-on-failure, a gate that
  fails closed, and others).
