**English** · [繁體中文](operations.zh-TW.md)

[Back to README](../README.md)

# Operations

*Historical: the bot is retired and no longer scheduled. These steps document how it was run.*

## Install and keys

```bash
pip install -r requirements.txt
cp .env.example ~/.env        # fill in your keys
```

Keys required in `~/.env`: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `HF_API_TOKEN`.
See `.env.example`.

## Cloudflare Workers AI fallback (recommended)

Set `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`.
The token needs only **Account > Workers AI > Read**.
This gives the sentiment leg a free fallback on a separate billing rail.
Without them the leg is Hugging Face only, and a spent credit balance takes the whole thing down at once.
`HF_SENTIMENT_MODEL` overrides the primary model without touching the code.

## Claude Sonnet via subscription (optional)

This runs the risk/exit gate on Claude **Sonnet** through the Claude Agent SDK.
It draws on your **Claude Pro plan's included usage** rather than metered Haiku API tokens.
Add `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) to turn it on.

Tunables: `CLAUDE_SDK_FOR` (`exits` [default] | `all` | `none`) and `CLAUDE_SDK_MODEL` (default `sonnet`).
With no token the bot runs Haiku-only, exactly as before.

## DeepSeek via deeperseeker

*Optional.* `USE_DEEPSEEK=1` makes DeepSeek V4.1 Flash the first leg for the analyst and the exit analyst.
It is reached through *deeperseeker*, a local OpenAI-compatible reverse proxy (default `http://127.0.0.1:4000/v1`).
The proxy fronts a **throwaway DeepSeek web-chat account**.

That use is against DeepSeek's ToS, so expect the account to be banned and its token to expire without notice.
Both show up as HTTP errors (401/403/429/5xx).
The proxy falls over under concurrency, so DeepSeek calls are strictly **sequential**.
A per-run **circuit breaker** stops calling DeepSeek for the rest of the run after 3 consecutive failures.
This caps an outage's cost at a few timeouts.

Fallback order: DeepSeek → agy → Gemini API chain.
When DeepSeek is down the bot loses only its primary leg.

Tunables: `DEEPSEEK_BASE` (default `http://127.0.0.1:4000/v1`), `DEEPSEEK_MODEL` (default `v4.1flash`), `DEEPSEEK_API_KEY` (required whenever `USE_DEEPSEEK=1`), `DEEPSEEK_TIMEOUT` (default 75 s).

This is separate from the sentiment leg's `deepseek-ai/DeepSeek-V4.1-Flash` on Hugging Face (step 3 of the [pipeline](pipeline.md)).
The JSON fields keep their `gemini_*` names for log compatibility whichever model answered.

## Gemini via Antigravity subscription

*Optional.* `USE_AGY_GEMINI=1` routes the analyst and exit analyst through the `agy` CLI.
It draws on a Google AI subscription instead of the Gemini API key.
That quota meters compute and resets **weekly**, so exhausting it returns a multi-day lockout rather than a next-day reset.
Any agy failure falls back to the API chain automatically.
When DeepSeek is also enabled, agy is the second leg, tried only after DeepSeek fails.

Tunables: `AGY_MODEL` (default `Gemini 3.8 Flash (High)`), `AGY_BIN`, `AGY_TIMEOUT`.

## Quota valve

*Optional.* `EXIT_GATE=1` limits the LLM exit review to positions trading below their 5-day moving average.
Measured across 19,654 historical reviews, this removes ~65% of exit-analyst calls while still surfacing 73% of the exits that actually executed.
It fails open when market data is unusable.
Off by default: the trade is only worth making when quota, not accuracy, is the binding constraint.

## Cost logging

Every metered Claude call is appended to `trade_logs/llm_calls.jsonl` with model, path, call type, token counts and cost.
`llm_cost_report.py` summarises it (`--days N` for a per-day breakdown, `--days 0` for the whole file).
Metered and subscription calls are reported separately and never summed.
Exit reviews additionally store the exact prompt they were given, so the component can be evaluated after the fact rather than reconstructed from other logs.

## Run

```bash
python3 trader.py
```

The script enforces its own 9:30–16:00 ET market-hours window (and skips holidays via Alpaca's clock endpoint), so it exits early when the market is closed.

## Cron (every 30 min during market hours)

```cron
*/30 9-15 * * 1-5 flock -n /tmp/trader.lock /usr/bin/python3 /path/to/trader.py >> /path/to/trade_logs/cron.log 2>&1
```

`flock -n` keeps overlapping runs from stacking up if one is slow.
Adjust the hour range to your server's timezone; the bot self-enforces the ET window regardless.

## Healthcheck

Arm `healthcheck.py` on the same cadence.
It is external on purpose: the outage that motivated it crashed `trader.py` at *import* time, before any in-process guard could run.
So the only durable signal is whether a run finished recently.

It raises two independent alarms:

- **Stale**: `run_log.jsonl` stops advancing during market hours.
- **Degraded**: a run completed but the whole sentiment chain fell through and every ticker was scored NEUTRAL/0.

The degraded alarm is rate-limited.
It deliberately leaves the stale marker and the exit code alone, because the bot is alive; it is just flying blind.

```cron
*/30 9-15 * * 1-5 HEALTHCHECK_NTFY_TOPIC=your-topic /usr/bin/python3 /path/to/healthcheck.py
```

Tunables: `HEALTHCHECK_MAX_AGE_MIN` (default 90), `HEALTHCHECK_DEGRADED_REALERT_H` (default 12; also the DeepSeek re-alert window), `HEALTHCHECK_NTFY_TOPIC`, `HEALTHCHECK_WEBHOOK`, `HEALTHCHECK_SMTP_FILE` (path of the email config below).
With no channel configured it still writes `trade_logs/healthcheck.log` and drops a marker file, and sends nothing outward.
The rate-limit markers are still written, so it does not repeat.

## Healthcheck markers and retries

The marker is written only once an alert was actually delivered (or no channel is configured).
So when every channel (ntfy, webhook, email) fails, the alert is retried on the next run instead of going silent for 12 h.
The `healthcheck.log` line is written either way.
This holds for the degraded alert too.

## DeepSeek alerts

A third, independent alert covers DeepSeek (only when the run reports it enabled).

**DeepSeek down.** When the circuit breaker tripped in the last run, healthcheck sends a **DeepSeek down** message with the last error.
When that error text contains `HTTP 401` or `HTTP 403` (trader.py formats errors as `deepseek HTTP 401: ...`), it adds a hint that the userToken probably expired or was banned and needs a re-auth.
Other errors, such as a timeout or a `max_tokens` complaint, get no hint.
It keeps its own marker, `trade_logs/DEEPSEEK_DOWN`, so it is rate-limited separately from the degraded alert (same 12 h window).

**DeepSeek recovered.** When DeepSeek answers again, healthcheck sends one **DeepSeek recovered** message.
It does so only after a clean run (at least one successful call and no failed ones, breaker not tripped).
The marker is not deleted: it is rewritten as `<original alert timestamp> RECOVERED`.
The original timestamp keeps rate-limiting the down alert.
The `RECOVERED` tag keeps the recovered message to once per outage.
So a flapping DeepSeek (down, up, down, ...) alerts at most once per 12 h and sends at most one recovered message for that outage.
A later alert, once the window has passed, replaces the marker and arms the next recovered message.
A run that never called DeepSeek, or that still saw failures, leaves the marker alone.

Like the degraded alert, it does not touch the stale marker or the exit code, because the bot has already fallen back to agy/Gemini and kept trading.

## Email (Gmail SMTP)

Every alert (stale, degraded, DeepSeek down, recovered) is also emailed when a root-only config file exists.
It is a file, not env vars, because a crontab shows up in listings and the password would leak with it.
The path is `$HEALTHCHECK_SMTP_FILE`, default `/root/.healthcheck-smtp`.
If the file is missing, email is simply off.

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

`SMTP_HOST` (default `smtp.gmail.com`), `SMTP_PORT` (default 587, STARTTLS; 465 uses implicit TLS) and `SMTP_FROM` (default `SMTP_USER`) are optional.
`SMTP_USER`, `SMTP_PASS` and `SMTP_TO` (comma-separated) are required, otherwise email stays off.
`SMTP_PASS` is a Google App Password (needs 2-Step Verification); the spaces Google shows in it are stripped.
Lines starting with `#` are comments and values may be quoted.

`--test-email` sends one test message through every configured channel and prints which accepted it.
It exits 0 if at least one did, else 1, and writes no marker file.
A failed email is logged to `healthcheck.log` as `EMAIL_FAIL <error>` (never the password).
It counts as delivered if ntfy or the webhook took the alert.

## ntfy email is not supported

Anonymous ntfy email is not supported.
ntfy.sh rejects the `Email:` header with HTTP 400 (`anonymous email sending is not allowed`) and drops the push along with it.
So `HEALTHCHECK_NTFY_EMAIL` no longer exists.
Use the SMTP file instead.

## Dashboard

A small Flask app in `dashboard/` showed positions, decisions, and history, in **zh-TW and English** (switch at `/lang/<code>`; strings live in `i18n.py`).
The overview page charts portfolio value over time with a **high-water-mark line and drawdown shading**, and carries the holdings table.
`/positions` is kept as a redirect to `/` so old links still work.

Model names in the footer and on the decision cards are derived from the logs at render time, never hand-written, so they cannot drift from what the bot actually called.
Labels name the role (`Sentiment`) rather than the vendor, for the same reason: the provider behind that leg changes.
The analyst badges (`DeepSeek: BUY`, `Gemini: HOLD`) are the one place a vendor is named.
It is the provider that actually answered, read from each row's model label (`ds:` → DeepSeek, `agy:`, `cli:` or `gemini…` → Gemini; empty, `none` or anything else → the generic `Analyst`).
Static column headers just say `Analyst`.

```bash
python3 dashboard/app.py
```

The public instance at trader.hydr0negnetwork.de has been replaced by a static retirement page.
