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
   Walks a model-priority chain (`gemini-3.5-flash` → `gemini-3.1-pro` → … →
   `gemini-2.5-flash`) so it degrades gracefully when a model is quota-gated.
3. **Sentiment** (Hugging Face) — BULLISH/BEARISH/NEUTRAL second opinion.
4. **Risk** (Claude) — final gate; vetoes unsafe trades. Uses **Sonnet** via the
   Claude Agent SDK (free subscription credit) for the crucial exit decisions and
   the **Haiku** API for the high-volume buy screen; falls back to Haiku whenever
   the credit is depleted or no subscription token is configured.
5. **Execution** — Alpaca paper order; hard stop-loss floor enforced independently
   of the LLMs.
6. **Exit analysis** — open positions are re-evaluated by a separate Gemini **API**
   exit analyst + a Claude exit-risk gate. (Google retired the individual-tier
   `gemini-cli` on 2026-06-18; that path is off by default — set
   `USE_GEMINI_EXIT_CLI=1` only with a paid-key-backed CLI.)

A prompt rule forbids the analyst from inventing news when no headlines are supplied —
absence of data must be stated, not hallucinated.

## Safety rails (LLM-independent)

| Rail | Default | Env override |
|------|---------|--------------|
| Hard stop-loss | 5% | `STOP_LOSS_PCT` |
| Position size | 2% of equity | `POSITION_SIZE_PCT` |
| Max concurrent positions | 8 | `MAX_POSITIONS` |
| Tickers per batch | 10 | `TICKER_BATCH_SIZE` |

## Setup

```bash
pip install -r requirements.txt
cp .env.example ~/.env        # fill in your keys
```

Keys required in `~/.env`: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `GEMINI_API_KEY`,
`ANTHROPIC_API_KEY`, `HF_API_TOKEN`. See `.env.example`.

**Optional — Claude Sonnet via subscription.** To run the risk/exit gate on Claude
**Sonnet** through the Claude Agent SDK (drawing on the free ~$20/mo subscription
credit instead of metered Haiku API tokens), add `CLAUDE_CODE_OAUTH_TOKEN` (from
`claude setup-token`). Tunables: `CLAUDE_SDK_FOR` (`exits` [default] | `all` |
`none`), `CLAUDE_SDK_MONTHLY_CAP_USD` (default `20`), `CLAUDE_SDK_MODEL` (default
`sonnet`). The credit pool is shared with the sibling `DOWTrade` bot via
`~/.claude_sdk_credit.json`; with no token the bot runs Haiku-only, exactly as before.

## Run

```bash
python3 trader.py
```

The script enforces its own 9:30–16:00 ET market-hours window (and skips holidays
via Alpaca's clock endpoint), so it exits early when the market is closed.

### Cron (every 30 min during market hours)

```cron
*/30 9-15 * * 1-5 /usr/bin/python3 /path/to/trader.py >> /path/to/trade_logs/cron.log 2>&1
```

## Dashboard

A small Flask app in `dashboard/` shows positions, decisions, and history.

```bash
python3 dashboard/app.py
```

## Layout

```
trader.py              # main pipeline (data → analyst → sentiment → risk → execute → exit)
requirements.txt
dashboard/
  app.py               # Flask dashboard
  templates/           # overview, positions, decisions, history
.env.example           # credential template (real keys live in ~/.env, never committed)
```
