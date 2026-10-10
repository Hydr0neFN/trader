**English** · [繁體中文](README.zh-TW.md)

# trader

> **Status: retired 2026-10-10.** The bot has been shut down and this repository
> is kept as a record. The live dashboard at trader.hydr0negnetwork.de has been
> replaced by a static retirement page. The paper positions were **frozen, not
> liquidated**. The sister project [DOWTrade](https://github.com/Hydr0neFN/DOWTrade)
> is **not** retired and is still running.

## Final results

Alpaca paper account, $100,000 start, 2026-04-14 to the 2026-10-09 close (120
trading days). Final equity, cash and the 24 open positions were verified against
Alpaca on 2026-10-10.

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

### Why it was retired

The owner's decision, 2026-10-10:

1. **The research question is unanswerable on any useful timescale.** Proving
   alpha would take about 16 years of live running.
2. **It underperformed buy-and-hold SPY by 5.6 pp** with lower beta; risk-adjusted
   it also trailed (Sharpe 1.55 vs 2.08).
3. **The LLM path was a maintenance cost with no research return.** It drained the
   owner's main-account Gemini quota (~320 calls/day), and the provider chain kept
   degrading (Hugging Face credits, agy quota, DeepSeek-proxy ban risk).

### What it taught

- A fallback that shares its primary's billing rail is not a fallback.
- Alert on degraded LLM output, not only on outright failure.
- Timezone and cron traps are real.
- Run a power analysis before "let it run to find out".
- Four mechanisms biased the bot toward inaction (HOLD-on-failure, a gate that
  fails closed, and others).

The rest of this README documents the system as it was built and is left intact as
the record. It is written in the past tense where it describes live operation.

Multi-LLM algorithmic **paper-trading** system for US equities. It ran on a cron
schedule during market hours, analyzed ~50 large-cap S&P 500 names with an
ensemble of language models, and placed paper orders through Alpaca.

> **Paper-only.** All trading uses Alpaca's paper endpoint (`paper=True`). No real
> money is at risk. No profitability is claimed — this was a research scaffold.

## How it works

Each run (every 30 min, 9:30–16:00 ET, weekdays) executed a pipeline per ticker batch:

1. **Market data + news** — price history via yfinance, headlines via Alpaca news API.
2. **Analyst** (DeepSeek → agy → Gemini) — BUY/SELL/HOLD recommendation with
   confidence + reasoning. With `USE_DEEPSEEK=1` it asks DeepSeek V4.1 Flash first,
   through a local reverse proxy (see *DeepSeek via deeperseeker* below); then agy
   (`USE_AGY_GEMINI=1`, a Gemini subscription); then the Gemini API, which walks a
   model-priority chain (`gemini-3.8-flash` → `gemini-3.6-flash` → … →
   `gemini-3.1-flash-lite`) so it degrades gracefully when a model is quota-gated.
   Each decision stores the model that answered it: `ds:v4.1flash`,
   `agy:Gemini 3.8 Flash (High)`, `gemini-…`, or `none` for a failed batch.
3. **Sentiment** (DeepSeek → Cloudflare Workers AI) — BULLISH/BEARISH/NEUTRAL
   second opinion. It only blocks a trade when it **directly contradicts** the
   analyst (BUY vs BEARISH, or SELL vs BULLISH); NEUTRAL (quiet/empty news) does
   not veto. The chain is `deepseek-ai/DeepSeek-V4.1-Flash` on Hugging Face, then
   `@cf/mistralai/mistral-small-3.1-24b-instruct` and
   `@cf/meta/llama-4-scout-17b-16e-instruct` on Cloudflare Workers AI. The two
   fallbacks are deliberately on a **different billing rail**: the old chain was
   four Hugging Face models, which meant one exhausted credit balance returned 402
   for every one of them at once. Cloudflare's free tier is 10,000 Neurons/day,
   roughly 900 sentiment calls, so the leg survives a spent Hugging Face balance.
   Models that emit their answer in `reasoning` and leave `content` empty are
   unusable here and were excluded by measurement, not by reputation.
4. **Risk** (Claude) — final gate; vetoes unsafe trades. Uses **Sonnet** via the
   Claude Agent SDK — drawing on your **Claude Pro plan's included usage** — for the
   crucial exit decisions, and the **Haiku** API for the high-volume buy screen;
   falls back to Haiku when no subscription token is configured.
5. **Execution** — Alpaca paper order; a hard stop-loss floor **and a
   profit-protecting trailing stop** are enforced independently of the LLMs.
6. **Exit analysis** — open positions are re-evaluated by an exit analyst and a
   Claude exit-risk gate. The exit analyst uses the same order as step 2: DeepSeek
   (`USE_DEEPSEEK=1`), then the Antigravity CLI against a Google AI **subscription**
   (`USE_AGY_GEMINI=1`), then the Gemini API chain; each leg falls through to the next
   on any failure, and with neither flag set it uses the API chain directly. (Google
   retired the individual-tier `gemini-cli` on 2026-06-18; that path is off by default
   — set `USE_GEMINI_EXIT_CLI=1` only with a paid-key-backed CLI.)

   Note the asymmetry this creates: a `HOLD` returns before the Claude gate is
   consulted, so the gate can veto an unwarranted exit but cannot catch a missed one.

A prompt rule forbids the analyst from inventing news when no headlines are supplied —
absence of data must be stated, not hallucinated.

## Safety rails (LLM-independent)

<!-- RAILS:START -->
| Rail | Default | Env override |
|------|---------|--------------|
| Hard stop-loss | 5% | `STOP_LOSS_PCT` |
| Trailing stop (pullback from peak) | 3.5% | `TRAIL_STOP_PCT` |
| Trailing-stop activation (gain before it arms) | 3% | `TRAIL_ACTIVATE_PCT` |
| Position size | 2% of equity | `POSITION_SIZE_PCT` |
| Max concurrent positions | 8 | `MAX_POSITIONS` |
| Cash floor never spent | $500 | `CASH_RESERVE_USD` |
| Minimum size for a cash-trimmed order | $750 | `MIN_TRADE_USD` |
| Tickers per batch | 10 | `TICKER_BATCH_SIZE` |
| Analyst confidence floor | 70% | — (hardcoded) |
<!-- RAILS:END -->

The table above is generated, not typed: `tools/readme_rails.py` parses
`trader.py` with `ast` and rewrites the block between the `RAILS` markers. Run
`--check` before committing (exit 1 on drift) and `--write` to regenerate. It
reads the AST rather than grepping the text because a regex cannot tell an
active call from a commented-out one, silently takes the first of two calls that
disagree, and gives up on any default that is not a plain string literal — every
one of which can publish a number the program never uses. Two live reads of the
same variable with different defaults are an error, not a coin flip.
Every stale number this project has published — a hardcoded `/20` on the GitHub
profile, a hand-written model list in the dashboard footer — was a value someone
had copied. Copying is the bug; deriving is the fix.


A BUY or SELL below the confidence floor is vetoed with the reason recorded, the
same way a Haiku veto or a contradicting sentiment read is. It has no env
override on purpose: measurement across this bot's own history found the
confidence score has no predictive value for outcome, so it is kept as a crude
floor rather than promoted to a ranking key.

Position sizing is a percentage of *portfolio value*, which says nothing about
settled cash. Until the **cash guard** was added, `MAX_POSITIONS x
POSITION_SIZE_PCT <= 100%` was the only thing keeping the bot off margin — and a
paper account is typically handed ~4x buying power, so an over-budget order fills
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

## Setup

*Historical: the bot is retired and no longer scheduled. These steps document how it was run.*

```bash
pip install -r requirements.txt
cp .env.example ~/.env        # fill in your keys
```

Keys required in `~/.env`: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `GEMINI_API_KEY`,
`ANTHROPIC_API_KEY`, `HF_API_TOKEN`. See `.env.example`.

**Recommended — Cloudflare Workers AI fallback.** Set `CLOUDFLARE_ACCOUNT_ID` and
`CLOUDFLARE_API_TOKEN` (the token needs only **Account > Workers AI > Read**) to give
the sentiment leg a free fallback on a separate billing rail. Without them the leg is
Hugging Face only, and a spent credit balance takes the whole thing down at once.
`HF_SENTIMENT_MODEL` overrides the primary model without touching the code.

**Optional — Claude Sonnet via subscription.** To run the risk/exit gate on Claude
**Sonnet** through the Claude Agent SDK — drawing on your **Claude Pro plan's
included usage** rather than metered Haiku API tokens — add `CLAUDE_CODE_OAUTH_TOKEN`
(from `claude setup-token`). Tunables: `CLAUDE_SDK_FOR` (`exits` [default] | `all` |
`none`) and `CLAUDE_SDK_MODEL` (default `sonnet`). With no token the bot runs
Haiku-only, exactly as before.

**Optional — DeepSeek via deeperseeker.** `USE_DEEPSEEK=1` makes DeepSeek V4.1 Flash
the first leg for the analyst and the exit analyst. It is reached through
*deeperseeker*, a local OpenAI-compatible reverse proxy (default
`http://127.0.0.1:4000/v1`) that fronts a **throwaway DeepSeek web-chat account**.
That use is against DeepSeek's ToS, so expect the account to be banned and its token
to expire without notice; both show up as HTTP errors (401/403/429/5xx). The proxy
falls over under concurrency, so DeepSeek calls are strictly **sequential**. A
per-run **circuit breaker** stops calling DeepSeek for the rest of the run after 3
consecutive failures, which caps an outage's cost at a few timeouts. Fallback order:
DeepSeek → agy → Gemini API chain; when DeepSeek is down the bot loses only its
primary leg. Tunables: `DEEPSEEK_BASE` (default `http://127.0.0.1:4000/v1`),
`DEEPSEEK_MODEL` (default `v4.1flash`), `DEEPSEEK_API_KEY` (required whenever
`USE_DEEPSEEK=1`), `DEEPSEEK_TIMEOUT` (default 75 s). This is separate from the
sentiment leg's `deepseek-ai/DeepSeek-V4.1-Flash` on Hugging Face (step 3). The JSON
fields keep their `gemini_*` names for log compatibility whichever model answered.

**Optional — Gemini via Antigravity subscription.** `USE_AGY_GEMINI=1` routes the
analyst and exit analyst through the `agy` CLI, drawing on a Google AI subscription
instead of the Gemini API key. That quota meters compute and resets **weekly**, so
exhausting it returns a multi-day lockout rather than a next-day reset; any agy
failure falls back to the API chain automatically. When DeepSeek is also enabled, agy
is the second leg, tried only after DeepSeek fails. Tunables: `AGY_MODEL` (default
`Gemini 3.8 Flash (High)`), `AGY_BIN`, `AGY_TIMEOUT`.

**Optional — quota valve.** `EXIT_GATE=1` limits the LLM exit review to positions
trading below their 5-day moving average. Measured across 19,654 historical reviews
this removes ~65% of exit-analyst calls while still surfacing 73% of the exits that
actually executed; it fails open when market data is unusable. Off by default: the
trade is only worth making when quota, not accuracy, is the binding constraint.

**Cost logging.** Every metered Claude call is appended to
`trade_logs/llm_calls.jsonl` with model, path, call type, token counts and cost.
`llm_cost_report.py` summarises it (`--days N` for a per-day breakdown, `--days 0`
for the whole file). Metered and subscription calls are reported separately and never
summed. Exit reviews additionally store the exact prompt they were given, so the
component can be evaluated after the fact rather than reconstructed from other logs.

## Run

```bash
python3 trader.py
```

The script enforces its own 9:30–16:00 ET market-hours window (and skips holidays
via Alpaca's clock endpoint), so it exits early when the market is closed.

### Cron (every 30 min during market hours)

```cron
*/30 9-15 * * 1-5 flock -n /tmp/trader.lock /usr/bin/python3 /path/to/trader.py >> /path/to/trade_logs/cron.log 2>&1
```

`flock -n` keeps overlapping runs from stacking up if one is slow. Adjust the hour
range to your server's timezone — the bot self-enforces the ET window regardless.

Arm `healthcheck.py` on the same cadence. It is external on purpose: the outage
that motivated it crashed `trader.py` at *import* time, before any in-process
guard could run, so the only durable signal is whether a run finished recently.
It raises two independent alarms — **stale**, when `run_log.jsonl` stops
advancing during market hours, and **degraded**, when a run completed but the
whole sentiment chain fell through and every ticker was scored NEUTRAL/0. The
second is rate-limited and deliberately leaves the stale marker and the exit
code alone, because the bot is alive; it is just flying blind.

A third, independent alert covers DeepSeek (only when the run reports it enabled).
When the circuit breaker tripped in the last run, healthcheck sends a **DeepSeek
down** message with the last error; when that error text contains `HTTP 401` or
`HTTP 403` (trader.py formats errors as `deepseek HTTP 401: ...`) it adds a hint that
the userToken probably expired or was banned and needs a re-auth. Other errors, such
as a timeout or a `max_tokens` complaint, get no hint. It keeps its own marker,
`trade_logs/DEEPSEEK_DOWN`, so it is rate-limited separately from the degraded alert
(same 12 h window).

The marker is written only once an alert was actually delivered (or no channel is
configured), so when every channel (ntfy, webhook, email) fails the alert is retried on the next run instead of going
silent for 12 h; the `healthcheck.log` line is written either way. This holds for the
degraded alert too. When DeepSeek answers again, healthcheck sends one **DeepSeek
recovered** message, but only after a clean run (at least one successful call and no
failed ones, breaker not tripped). The marker is not deleted: it is rewritten as
`<original alert timestamp> RECOVERED`. The original timestamp keeps rate-limiting the
down alert and the `RECOVERED` tag keeps the recovered message to once per outage, so
a flapping DeepSeek (down, up, down, ...) alerts at most once per 12 h and sends at
most one recovered message for that outage. A later alert, once the window has passed,
replaces the marker and arms the next recovered message. A run that never called
DeepSeek, or that still saw failures, leaves the marker alone. Like the degraded
alert, it does not touch the stale marker or the exit code, because the bot has
already fallen back to agy/Gemini and kept trading.

```cron
*/30 9-15 * * 1-5 HEALTHCHECK_NTFY_TOPIC=your-topic /usr/bin/python3 /path/to/healthcheck.py
```

Tunables: `HEALTHCHECK_MAX_AGE_MIN` (default 90), `HEALTHCHECK_DEGRADED_REALERT_H`
(default 12; also the DeepSeek re-alert window), `HEALTHCHECK_NTFY_TOPIC`,
`HEALTHCHECK_WEBHOOK`, `HEALTHCHECK_SMTP_FILE` (path of the email config below). With
no channel configured it still writes `trade_logs/healthcheck.log` and drops a marker
file, and sends nothing outward; the rate-limit markers are still written, so it does
not repeat.

**Email (Gmail SMTP).** Every alert (stale, degraded, DeepSeek down, recovered) is
also emailed when a root-only config file exists. It is a file, not env vars, because
a crontab shows up in listings and the password would leak with it. The path is
`$HEALTHCHECK_SMTP_FILE`, default `/root/.healthcheck-smtp`; if the file is missing,
email is simply off.

```ini
# /root/.healthcheck-smtp  (chmod 600)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASS="abcd efgh ijkl mnop"
SMTP_TO=you@gmail.com, other@example.com
SMTP_FROM=you@gmail.com
```

```bash
chmod 600 /root/.healthcheck-smtp
python3 /path/to/healthcheck.py --test-email
```

`SMTP_HOST` (default `smtp.gmail.com`), `SMTP_PORT` (default 587, STARTTLS; 465 uses
implicit TLS) and `SMTP_FROM` (default `SMTP_USER`) are optional; `SMTP_USER`,
`SMTP_PASS` and `SMTP_TO` (comma-separated) are required, otherwise email stays off.
`SMTP_PASS` is a Google App Password (needs 2-Step Verification); the spaces Google
shows in it are stripped. Lines starting with `#` are comments and values may be
quoted. `--test-email` sends one test message through every configured channel,
prints which accepted it, and exits 0 if at least one did, else 1; it writes no
marker file. A failed email is logged to `healthcheck.log` as `EMAIL_FAIL <error>`
(never the password) and counts as delivered if ntfy or the webhook took the alert.

Anonymous ntfy email is not supported: ntfy.sh rejects the `Email:` header with HTTP
400 (`anonymous email sending is not allowed`) and drops the push along with it, so
`HEALTHCHECK_NTFY_EMAIL` no longer exists. Use the SMTP file instead.

## Dashboard

A small Flask app in `dashboard/` showed positions, decisions, and history, in
**zh-TW and English** (switch at `/lang/<code>`; strings live in `i18n.py`). The
overview page charts portfolio value over time with a **high-water-mark line and
drawdown shading**, and carries the holdings table — `/positions` is kept as a
redirect to `/` so old links still work.

Model names in the footer and on the decision cards are derived from the logs at
render time, never hand-written, so they cannot drift from what the bot actually
called. Labels name the role (`Sentiment`) rather than the vendor, for the same
reason: the provider behind that leg changes. The analyst badges (`DeepSeek: BUY`,
`Gemini: HOLD`) are the one place a vendor is named, and it is the provider that
actually answered, read from each row's model label (`ds:` → DeepSeek, `agy:`,
`cli:` or `gemini…` → Gemini; empty, `none` or anything else → the generic `Analyst`).
Static column headers just say `Analyst`.

```bash
python3 dashboard/app.py
```

The public instance at trader.hydr0negnetwork.de has been replaced by a static
retirement page.

## Layout

```
trader.py              # main pipeline (data → analyst → sentiment → risk → execute → exit)
llm_cost_report.py     # read-only summary of trade_logs/llm_calls.jsonl
tools/readme_rails.py  # derives the safety-rail table from trader.py (--check / --write)
healthcheck.py         # run-freshness + degraded-LLM + DeepSeek-down check, notifies via ntfy / webhook / email
requirements.txt
dashboard/
  app.py               # Flask dashboard
  i18n.py              # zh-TW / EN string table (shared byte-for-byte with DOWTrade)
  static/theme.css
  templates/           # overview, decisions, history + _lang_switch, _positions_table
.env.example           # credential template (real keys live in ~/.env, never committed)
```
