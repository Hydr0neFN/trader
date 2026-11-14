# Pipeline

Each run (every 30 min, 9:30–16:00 ET, weekdays) executed a pipeline per ticker
batch:

1. **Market data + news** — price history via yfinance, headlines via Alpaca news API.
2. **Analyst** (DeepSeek → agy → Gemini) — BUY/SELL/HOLD recommendation with
   confidence + reasoning. With `USE_DEEPSEEK=1` it asks DeepSeek V4.1 Flash first,
   through a local reverse proxy (see [DeepSeek via deeperseeker](operations.md));
   then agy (`USE_AGY_GEMINI=1`, a Gemini subscription); then the Gemini API, which
   walks a model-priority chain (`gemini-3.8-flash` → `gemini-3.6-flash` → … →
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
