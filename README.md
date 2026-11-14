**English** · [繁體中文](README.zh-TW.md)

# trader

A multi-LLM **paper-trading** bot for US equities. It ran on a cron schedule
during market hours, analyzed ~50 large-cap S&P 500 names with an ensemble of
language models, and placed paper orders through Alpaca. It is retired; this
repository is the record.

> **Status: retired 2026-10-10.** The bot has been shut down and this repository
> is kept as a record. The live dashboard at trader.hydr0negnetwork.de has been
> replaced by a static retirement page. The paper positions were **frozen, not
> liquidated**. The sister project [DOWTrade](https://github.com/Hydr0neFN/DOWTrade)
> is **not** retired and is still running.

> **Paper-only.** All trading uses Alpaca's paper endpoint (`paper=True`). No real
> money is at risk. No profitability is claimed — this was a research scaffold.

## At a glance

| Aspect | Detail |
|--------|--------|
| Problem | How a multi-LLM ensemble actually performs against buy-and-hold SPY over 120 trading days |
| What I built | A cron-driven paper-trading pipeline: market data and news, an analyst ensemble, a sentiment check, a Claude risk gate, and Alpaca paper execution |
| Result | +7.06% over 120 trading days ($107,064.60 from $100,000) against SPY +12.68%, a gap of -5.62 pp; the alpha 95% CI runs from -18.3% to +26.1% a year |
| Stack | Python, Alpaca, yfinance, DeepSeek / agy / Gemini, Claude Agent SDK, Cloudflare Workers AI, Flask |
| Status | Retired 2026-10-10; positions frozen, not liquidated |

## How it works

Each run pushed one ticker batch through the same chain:

```
cron: every 30 min, 9:30-16:00 ET, weekdays
  -> market data + news     yfinance, Alpaca news API
  -> analyst                DeepSeek -> agy -> Gemini API chain
  -> sentiment              DeepSeek (Hugging Face) -> Cloudflare Workers AI
  -> risk gate              Claude Sonnet (exits) / Haiku (buy screen)
  -> execution              Alpaca paper order, hard stop + trailing stop
  -> exit analysis          exit analyst + Claude exit-risk gate
```

- The analyst returns BUY, SELL or HOLD with a confidence score and reasoning.
- The sentiment leg blocks a trade only when it directly contradicts the analyst.
- A prompt rule forbids the analyst from inventing news when no headlines are supplied.
- The full chain, model names, fallbacks and tunables are in
  [docs/pipeline.md](docs/pipeline.md).

## Safety rails (LLM-independent)

Stop-loss, position and cash limits are enforced by code, not by the LLMs.

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

The table is generated from `trader.py` by `tools/readme_rails.py`; run `--check`
before committing (exit 1 on drift) and `--write` to regenerate it. The
confidence floor, the cash guard and the trailing stop are explained in
[docs/safety-rails.md](docs/safety-rails.md).

## Results and retirement

- **Symptom.** From 2026-04-14 to the 2026-10-09 close the bot returned +7.06%;
  SPY returned +12.68% over the same 120 trading days.
- **Measurement.** The daily regression (n=124) gives beta 0.417, alpha +3.9%/yr
  and t = 0.35. The 95% CI is [-18.3%, +26.1%] a year, so the estimate is
  indistinguishable from zero. Proving alpha at t=2 would take ~16 years.
- **Cause.** Risk-adjusted it also trailed (Sharpe 1.55 vs 2.08). The LLM path
  was a maintenance cost with no research return, and four mechanisms biased the
  bot toward inaction (HOLD-on-failure, a gate that fails closed, and others).
- **Fix.** The owner retired it on 2026-10-10. The positions were frozen, not
  liquidated, and the repository is kept as the record.
- **Still open.** The research question has no answer on a useful timescale.

The full result table, data notes and retirement reasons are in
[docs/results.md](docs/results.md).

## Build / run

*Historical: the bot is retired and no longer scheduled. These steps document how it was run.*

```bash
pip install -r requirements.txt
cp .env.example ~/.env        # fill in your keys
python3 trader.py
```

The script enforces its own 9:30-16:00 ET market-hours window and skips holidays
via Alpaca's clock endpoint, so it exits early when the market is closed. Keys,
optional providers and the cron/healthcheck setup are in
[docs/operations.md](docs/operations.md).

## Repo layout

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

## Limits and open items

- The bot underperformed buy-and-hold SPY by 5.6 pp, and risk-adjusted it also
  trailed (Sharpe 1.55 vs 2.08).
- Gaps: 2026-06-30 to 2026-07-13 was an outage on the old Pi (7 trading days,
  positions held), and there were no runs on 2026-08-26.
- 2026-10-09 ran degraded (agy quota out, Gemini flash-lite fallback). Excluding
  it the result is +6.27% vs SPY +12.01%, so the headline does not change.
- It is a paper account, so the 24 frozen positions keep marking to market.

## License

MIT License. See [LICENSE](LICENSE).
