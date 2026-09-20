**English** · [繁體中文](README.zh-TW.md)

# trader

Multi-LLM algorithmic **paper-trading** system for US equities. Runs on a cron
schedule during market hours, analyzes ~50 large-cap S&P 500 names with an
ensemble of language models, and places paper orders through Alpaca.

> **Paper-only.** All trading uses Alpaca's paper endpoint (`paper=True`). No real
> money is at risk. No profitability is claimed — this is a research scaffold.

## How it works

Each run (every 30 min, 9:30–16:00 ET, weekdays) executes a pipeline per ticker batch:

1. **Market data + news** — price history via yfinance, headlines via Alpaca news API.
2. **Analyst** (Gemini) — BUY/SELL/HOLD recommendation with confidence + reasoning.
   Walks a model-priority chain (`gemini-3.8-flash` → `gemini-3.6-flash` → … →
   `gemini-3.1-flash-lite`) so it degrades gracefully when a model is quota-gated.
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
6. **Exit analysis** — open positions are re-evaluated by a Gemini exit analyst and a
   Claude exit-risk gate. With `USE_AGY_GEMINI=1` the exit analyst goes through the
   Antigravity CLI against a Google AI **subscription** and falls back to the Gemini
   API chain on any failure; otherwise it uses the API chain directly. (Google retired
   the individual-tier `gemini-cli` on 2026-06-18; that path is off by default — set
   `USE_GEMINI_EXIT_CLI=1` only with a paid-key-backed CLI.)

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

The table above is generated, not typed: `tools/readme_rails.py` reads the
defaults out of `trader.py` and rewrites the block between the `RAILS` markers.
Run `--check` before committing (exit 1 on drift) and `--write` to regenerate.
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

**Optional — Gemini via Antigravity subscription.** `USE_AGY_GEMINI=1` routes the
analyst and exit analyst through the `agy` CLI, drawing on a Google AI subscription
instead of the Gemini API key. That quota meters compute and resets **weekly**, so
exhausting it returns a multi-day lockout rather than a next-day reset; any agy
failure falls back to the API chain automatically. Tunables: `AGY_MODEL` (default
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

```cron
*/30 9-15 * * 1-5 HEALTHCHECK_NTFY_TOPIC=your-topic /usr/bin/python3 /path/to/healthcheck.py
```

Tunables: `HEALTHCHECK_MAX_AGE_MIN` (default 90), `HEALTHCHECK_DEGRADED_REALERT_H`
(default 12), `HEALTHCHECK_NTFY_TOPIC`, `HEALTHCHECK_WEBHOOK`. With no channel
configured it still writes `trade_logs/healthcheck.log` and drops a marker file,
and sends nothing outward.

## Dashboard

A small Flask app in `dashboard/` shows positions, decisions, and history, in
**zh-TW and English** (switch at `/lang/<code>`; strings live in `i18n.py`). The
overview page charts portfolio value over time with a **high-water-mark line and
drawdown shading**, and carries the holdings table — `/positions` is kept as a
redirect to `/` so old links still work.

Model names in the footer and on the decision cards are derived from the logs at
render time, never hand-written, so they cannot drift from what the bot actually
called. Labels name the role (`Sentiment`) rather than the vendor, for the same
reason: the provider behind that leg changes.

```bash
python3 dashboard/app.py
```

## Layout

```
trader.py              # main pipeline (data → analyst → sentiment → risk → execute → exit)
llm_cost_report.py     # read-only summary of trade_logs/llm_calls.jsonl
tools/readme_rails.py  # derives the safety-rail table from trader.py (--check / --write)
healthcheck.py         # run-freshness + degraded-LLM check, notifies via ntfy
requirements.txt
dashboard/
  app.py               # Flask dashboard
  i18n.py              # zh-TW / EN string table (shared byte-for-byte with DOWTrade)
  static/theme.css
  templates/           # overview, decisions, history + _lang_switch, _positions_table
.env.example           # credential template (real keys live in ~/.env, never committed)
```
