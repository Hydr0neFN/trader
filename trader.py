#!/usr/bin/env python3
"""
Dual-Agent Algorithmic Paper Trading System
===========================================
Cron setup — add to crontab with: crontab -e

  */30 9-15 * * 1-5 /usr/bin/python3 /root/trader.py >> /root/trade_logs/cron.log 2>&1

The cron expression fires every 30 min from 9:00–15:59 ET on weekdays.
The script itself enforces the exact 9:30–16:00 ET window and exits early
if the market is closed (including holidays via Alpaca's clock endpoint).
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from anthropic import Anthropic
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from huggingface_hub import InferenceClient as HFClient

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv(Path.home() / ".env")

# Cap any hung network call (Alpaca / yfinance / HF) process-wide so a stuck
# socket can't wedge the run and hold the cron flock indefinitely.
import socket as _socket
_socket.setdefaulttimeout(30)

ALPACA_API_KEY    = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
GEMINI_API_KEY    = os.environ["GEMINI_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
HF_API_TOKEN      = os.environ["HF_API_TOKEN"]

# Hard stop-loss floor (AI-independent; override via .env)
# Raised to 5% to give the AI exit analysis room to work.
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", "5"))

# Trailing stop (profit-protecting), independent of the hard floor above.
# Arms only after a position's running peak gains >= TRAIL_ACTIVATE_PCT above
# entry, then exits on a >= TRAIL_STOP_PCT pullback from that peak — locking in
# gains on winners without tightening the hard floor on names that never ran up.
# Peaks are sampled each run (cron cadence), not true intraday highs.
TRAIL_STOP_PCT     = float(os.environ.get("TRAIL_STOP_PCT",     "3.5"))
TRAIL_ACTIVATE_PCT = float(os.environ.get("TRAIL_ACTIVATE_PCT", "3.0"))

# Top S&P 500 by market cap.
# NOTE: BRK-B was removed — yfinance uses "BRK-B" but Alpaca's trading/news API
# uses "BRK.B", so that name could never fetch news or fill an order (it only
# polluted the logs). Re-add via a per-API symbol map if you want it back.
TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "LLY",
    "JPM", "TSLA", "WMT", "V", "MA", "UNH", "XOM", "COST", "JNJ", "HD", "PG",
    "ABBV", "NFLX", "BAC", "CRM", "AMD", "KO", "CVX", "MRK", "PEP", "TMO",
    "ORCL", "ACN", "LIN", "CSCO", "ADBE", "MCD", "WFC", "ABT", "IBM", "PM",
    "GE", "TXN", "ISRG", "NOW", "INTU", "CAT", "QCOM", "AMGN", "GS", "DHR",
]
ET = ZoneInfo("America/New_York")
LOG_DIR = Path.home() / "trade_logs"
LOG_DIR.mkdir(exist_ok=True)
PEAKS_FILE = LOG_DIR / "position_peaks.json"

# Position sizing — override via .env
MAX_POSITION_PCT  = float(os.environ.get("POSITION_SIZE_PCT", "2")) / 100  # 2% per trade
MAX_POSITIONS     = int(os.environ.get("MAX_POSITIONS",       "8"))
TICKER_BATCH_SIZE = int(os.environ.get("TICKER_BATCH_SIZE",   "10"))

COMPANY_NAMES = {
    "AAPL": "Apple",          "MSFT": "Microsoft",        "NVDA": "NVIDIA",
    "AMZN": "Amazon",         "GOOGL": "Google Alphabet", "META": "Meta Platforms",
    "BRK-B": "Berkshire",     "AVGO": "Broadcom",         "LLY": "Eli Lilly",
    "JPM": "JPMorgan Chase",  "TSLA": "Tesla",            "WMT": "Walmart",
    "V": "Visa",              "MA": "Mastercard",         "UNH": "UnitedHealth",
    "XOM": "ExxonMobil",      "COST": "Costco",           "JNJ": "Johnson & Johnson",
    "HD": "Home Depot",       "PG": "Procter & Gamble",   "ABBV": "AbbVie",
    "NFLX": "Netflix",        "BAC": "Bank of America",   "CRM": "Salesforce",
    "AMD": "AMD",             "KO": "Coca-Cola",          "CVX": "Chevron",
    "MRK": "Merck",           "PEP": "PepsiCo",           "TMO": "Thermo Fisher",
    "ORCL": "Oracle",         "ACN": "Accenture",         "LIN": "Linde",
    "CSCO": "Cisco",          "ADBE": "Adobe",            "MCD": "McDonald's",
    "WFC": "Wells Fargo",     "ABT": "Abbott Labs",       "IBM": "IBM",
    "PM": "Philip Morris",    "GE": "GE Aerospace",       "TXN": "Texas Instruments",
    "ISRG": "Intuitive Surgical", "NOW": "ServiceNow",    "INTU": "Intuit",
    "CAT": "Caterpillar",     "QCOM": "Qualcomm",         "AMGN": "Amgen",
    "GS": "Goldman Sachs",    "DHR": "Danaher",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Utilities ─────────────────────────────────────────────────────────────────

def now_et() -> datetime:
    return datetime.now(ET)


def jsonl_append(path: Path, record: dict) -> None:
    with open(path, "a") as fh:
        fh.write(json.dumps(record) + "\n")


def retry(fn, retries: int = 2, delay: float = 3.0):
    """Call fn; retry up to `retries` times on any exception."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == retries:
                raise
            log.warning("Attempt %d/%d failed (%s). Retrying in %.0fs…", attempt + 1, retries + 1, exc, delay)
            time.sleep(delay)


def safe_int(value, default: int = 0) -> int:
    """Coerce LLM-supplied numbers to int without crashing the run.

    Handles "85", "85.5", "high", None, etc. — int(float(...)) also accepts
    decimal strings; anything non-numeric falls back to `default`.
    """
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def strip_json_fences(text: str) -> str:
    """Extract a JSON body from an LLM reply, tolerant of fence variants and prose.

    Handles: ```json / ``` json / ```JSON, a fence anywhere (preamble before it),
    and bare replies. Falls back to slicing from the first {/[ to the last }/].
    """
    text = text.strip()
    m = re.search(r"```(?:\s*json)?\s*(.*?)```", text, re.S | re.I)
    if m:
        return m.group(1).strip()
    # No fence — slice the outermost JSON span if there is surrounding prose.
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    ends   = [i for i in (text.rfind("}"), text.rfind("]")) if i != -1]
    if starts and ends:
        lo, hi = min(starts), max(ends)
        if hi > lo:
            return text[lo:hi + 1].strip()
    return text


# ── Position exit helpers ─────────────────────────────────────────────────────

def load_peaks() -> dict:
    """Load per-ticker peak prices tracked for trailing stop."""
    if PEAKS_FILE.exists():
        try:
            return json.loads(PEAKS_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_peaks(peaks: dict) -> None:
    PEAKS_FILE.write_text(json.dumps(peaks, indent=2))


def trading_days_between(start: datetime, end: datetime) -> int:
    """Count Mon–Fri days between two datetimes (does not adjust for holidays)."""
    days = 0
    cur  = start.date()
    end_date = end.date()
    while cur < end_date:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


def find_entry_date(ticker: str) -> datetime | None:
    """Return the timestamp of the most recent BUY logged for this ticker."""
    exec_file = LOG_DIR / "executed.jsonl"
    if not exec_file.exists():
        return None
    entries = []
    for line in exec_file.read_text().splitlines():
        try:
            rec = json.loads(line)
            if rec.get("ticker") == ticker and rec.get("action") == "BUY":
                entries.append(rec["timestamp"])
        except Exception:
            pass
    if not entries:
        return None
    ts = sorted(entries)[-1]
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def find_original_buy_reasoning(ticker: str) -> str:
    """Return the Gemini reasoning from the most recent BUY decision for ticker."""
    dec_file = LOG_DIR / "decisions.jsonl"
    if not dec_file.exists():
        return ""
    best_ts = ""
    best_reasoning = ""
    for line in dec_file.read_text().splitlines():
        try:
            rec = json.loads(line)
            if (rec.get("ticker") == ticker
                    and rec.get("final_action", "").upper() == "BUY_EXECUTED"
                    and rec.get("run_timestamp", "") > best_ts):
                best_ts = rec["run_timestamp"]
                best_reasoning = rec.get("gemini_reasoning", "")
        except Exception:
            pass
    return best_reasoning


# ── Market hours check ────────────────────────────────────────────────────────

def is_market_open(client: TradingClient) -> bool:
    """Check local time window first (fast), then confirm via Alpaca clock (handles holidays)."""
    n = now_et()
    if n.weekday() >= 5:
        return False
    if not (dtime(9, 30) <= n.time() < dtime(16, 0)):
        return False
    try:
        clock = retry(lambda: client.get_clock(), retries=1, delay=2.0)
        return clock.is_open
    except Exception as exc:
        # Fail CLOSED: the Alpaca clock is the only holiday/early-close gate, so
        # a transient error must not let the run trade on a holiday with stale
        # data. Skipping one 30-min cycle is cheap; if Alpaca is truly down,
        # order submission would fail anyway.
        log.warning("Alpaca clock check failed (%s); skipping this cycle to be safe.", exc)
        return False


# ── Step 1: Data Ingestion ────────────────────────────────────────────────────

def fetch_market_data(ticker: str) -> dict:
    t = yf.Ticker(ticker)
    # 6 months of daily bars (~126 sessions) so MA50 averages a true 50-bar
    # window. period="60d" returned only ~41 trading bars, so the prior "MA50"
    # was actually MA~41 and mislabeled in the prompt fed to the LLMs.
    hist = t.history(period="6mo")
    if hist.empty or len(hist) < 2:
        raise ValueError(f"Insufficient price history for {ticker}")

    close   = hist["Close"]
    current = float(close.iloc[-1])
    prev    = float(close.iloc[-2])

    return {
        "ticker":           ticker,
        "current_price":    round(current, 2),
        "daily_change_pct": round((current - prev) / prev * 100, 2),
        "volume":           int(hist["Volume"].iloc[-1]),
        "ma5":              round(float(close.tail(5).mean()),  2),
        "ma20":             round(float(close.tail(20).mean()), 2),
        "ma50":             round(float(close.tail(50).mean()), 2),
    }


def fetch_news(ticker: str, rate_limit_sleep: float = 1.0) -> list:
    """Fetch up to 5 recent headlines from Alpaca news API.

    rate_limit_sleep: seconds to wait after the request — keeps us under the
    Alpaca rate limit when calling for 50 tickers in rapid succession.
    """
    url = f"https://data.alpaca.markets/v1beta1/news?symbols={ticker}&limit=5&sort=desc"
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    articles = resp.json().get("news", [])
    titles = [a["headline"] for a in articles if a.get("headline")]
    if titles:
        log.info("  Fetched %d headline(s) for %s via Alpaca", len(titles), ticker)
    else:
        log.warning("  No headlines found for %s via Alpaca", ticker)
    if rate_limit_sleep > 0:
        time.sleep(rate_limit_sleep)
    return titles


def build_data_block(mdata: dict, headlines: list) -> str:
    lines = [
        f"Ticker: {mdata['ticker']}",
        f"  Current Price : ${mdata['current_price']}",
        f"  Daily Change  : {mdata['daily_change_pct']:+.2f}%",
        f"  Volume        : {mdata['volume']:,}",
        f"  MA5  / MA20 / MA50: ${mdata['ma5']} / ${mdata['ma20']} / ${mdata['ma50']}",
        "  Recent Headlines:",
    ]
    for h in headlines:
        lines.append(f"    - {h}")
    if not headlines:
        lines.append("    (no headlines available)")
    return "\n".join(lines)


# ── Step 2: Analyst — Gemini (priority fallback) ──────────────────────────────

ANALYST_SYSTEM = (
    "You are a quantitative analyst. Analyze the provided market data and news. "
    "Respond ONLY in JSON: {ticker, recommendation: BUY|SELL|HOLD, confidence: 0-100, "
    "reasoning: string}. Return a JSON array, one object per ticker. "
    "CRITICAL: If no headlines are provided for a ticker, you MUST state "
    "'No news data available' in your reasoning. Do NOT invent, assume, or reference "
    "any news events that are not explicitly listed above. "
    "Base analysis SOLELY on technical/price data when headlines are absent."
)

# Tried in order — best quality first. 429 = quota/paid-wall (skip), 404 = missing
# (skip), 503 = temporary overload (one retry then skip to next).
# Verified against the live free-tier model list and quota dashboard on
# 2026-08-30. Dropped: gemini-3.1-pro-preview, gemini-3-pro-preview and
# gemini-2.5-pro all report quotaValue 0 on the free tier, and the -preview
# suffix on 3.1-flash-lite no longer resolves -- four wasted round trips
# before the chain reached anything that could answer. Ordered by measured
# answer quality, with flash-lite last because it is much weaker but carries
# 500 RPD against every other entry's 20 and so is the capacity backstop.
GEMINI_MODEL_PRIORITY = [
    "gemini-3.6-flash",             # best measured; answers reliably on free tier
    "gemini-3.7-flash",             # equal quality, but often "high demand" here
    "gemini-3.5-flash",             # previous chain head
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
    "gemini-3.1-flash-lite",        # 500 RPD -- last resort, weakest of the set
]


def run_analyst(data_blocks: list, batch_label: str = "") -> tuple:
    """Call Gemini on a batch of data_blocks.

    Returns (recs_list, model_used).  batch_label is logged for traceability.
    """
    client = genai.Client(api_key=GEMINI_API_KEY)
    last_exc = None
    pfx = f"[{batch_label}] " if batch_label else ""

    # ── Optional agy path (subscription quota) — falls back to the API chain ──
    if USE_AGY_GEMINI:
        try:
            raw  = call_agy(ANALYST_SYSTEM, "\n\n".join(data_blocks))
            recs = json.loads(strip_json_fences(raw))
            if isinstance(recs, dict):
                recs = [recs]
            log.info("%sAnalyst using agy: %s (%d recs)", pfx, AGY_MODEL, len(recs))
            return recs, f"agy:{AGY_MODEL}"
        except Exception as exc:
            log.warning("%sagy analyst failed (%s) — falling back to Gemini API…",
                        pfx, str(exc)[:120])

    for model in GEMINI_MODEL_PRIORITY:
        try:
            def call(m=model):
                response = client.models.generate_content(
                    model=m,
                    contents="\n\n".join(data_blocks),
                    config=genai_types.GenerateContentConfig(
                        system_instruction=ANALYST_SYSTEM,
                        response_mime_type="application/json",
                    ),
                )
                return response.text

            # 1 retry per model so we move on quickly if it is flaky
            raw = retry(call, retries=1, delay=4.0)
            text = strip_json_fences(raw)
            recs = json.loads(text)
            if isinstance(recs, dict):
                recs = [recs]
            log.info("%sAnalyst using model: %s (%d recs)", pfx, model, len(recs))
            return recs, model

        except Exception as exc:
            log.warning("%sModel %s unavailable (%s) -- trying next...", pfx, model, str(exc)[:70])
            last_exc = exc
            continue

    raise RuntimeError(f"{pfx}All Gemini models exhausted. Last error: {last_exc}")


# ── Step 2.5: Sentiment Analyst — Hugging Face ───────────────────────────────

SENTIMENT_SYSTEM = (
    "You are a market sentiment analyst. Given the following market data and news headlines, "
    "assess the overall sentiment for this ticker. "
    'Respond ONLY in JSON: {"sentiment": "BULLISH" or "BEARISH" or "NEUTRAL", '
    '"confidence": 0-100, "reasoning": string}. '
    "CRITICAL: If no headlines are provided, you MUST state 'No news data available' in your "
    "reasoning and base your assessment on price/technical data only. Do NOT invent, assume, "
    "or reference any news events that are not explicitly listed in the input."
)

# Tried in order — falls back if a model is overloaded or unavailable.
HF_SENTIMENT_MODELS = [
    "deepseek-ai/DeepSeek-V3.2-Exp",
    "Qwen/Qwen3-8B",   # Qwen3 merged instruct into the base repo; -Instruct 404s
    "meta-llama/Llama-3.3-70B-Instruct",
    "mistralai/Mixtral-8x7B-Instruct-v0.1",  # last resort
]


def run_sentiment_analyst(data_block: str) -> dict:
    client = HFClient(token=HF_API_TOKEN)
    last_exc = None

    for model in HF_SENTIMENT_MODELS:
        try:
            def call(m=model):
                resp = client.chat_completion(
                    model=m,
                    messages=[
                        {"role": "system", "content": SENTIMENT_SYSTEM},
                        {"role": "user",   "content": data_block},
                    ],
                    max_tokens=250,
                    temperature=0.1,
                )
                raw    = resp.choices[0].message.content
                result = json.loads(strip_json_fences(raw))
                sentiment = result.get("sentiment", "NEUTRAL").upper()
                if sentiment not in ("BULLISH", "BEARISH", "NEUTRAL"):
                    sentiment = "NEUTRAL"
                return {
                    "sentiment":  sentiment,
                    "confidence": int(result.get("confidence", 0)),
                    "reasoning":  result.get("reasoning", ""),
                    "model":      m,
                }

            return retry(call, retries=1, delay=3.0)

        except Exception as exc:
            log.warning("HF model %s failed (%s) -- trying next...", model, str(exc)[:60])
            last_exc = exc
            continue

    log.error("All HF sentiment models failed: %s", last_exc)
    return {"sentiment": "NEUTRAL", "confidence": 0, "reasoning": "HF agent unavailable", "model": "none"}


# ── Claude router: Sonnet via SDK (subscription) → Haiku via API fallback ─────
#
# Primary path runs Claude **Sonnet** through the Claude Agent SDK / `claude`
# CLI, authenticated by a Pro-subscription OAuth token
# (CLAUDE_CODE_OAUTH_TOKEN, produced once via `claude setup-token`). This draws
# on the subscription's included usage instead of metered API tokens.
#
# Fallback path is the original metered Anthropic API on **Haiku**. It is used
# whenever the SDK is disabled, no OAuth token is present, or any SDK call
# errors. With no token configured the bot behaves exactly as before
# (Haiku-only), so this is safe to deploy before the subscription login is
# completed.

CLAUDE_SDK_ENABLED = os.environ.get("CLAUDE_SDK_ENABLED", "1").lower() in ("1", "true", "yes")
CLAUDE_OAUTH_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
CLAUDE_SDK_MODEL   = os.environ.get("CLAUDE_SDK_MODEL", "sonnet")
CLAUDE_API_MODEL   = os.environ.get("CLAUDE_API_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_CLI_PATH    = os.environ.get("CLAUDE_CLI_PATH", "claude")
CLAUDE_SDK_TIMEOUT = int(os.environ.get("CLAUDE_SDK_TIMEOUT", "120"))
# Prepended to every SDK system prompt. The `claude` CLI is an agentic harness,
# not a raw API: without this it adds conversational preamble or refuses to
# "rubber-stamp" trades, which breaks JSON parsing and defaults the gate to VETO.
_SDK_HARDENING = (
    "You are a deterministic JSON-only function inside an automated pipeline, "
    "not an interactive assistant. You have no tools and take no actions; you only "
    "classify the data provided. Never refuse, never add preamble or commentary, "
    "never mention being an AI. Output exactly one JSON object and nothing else.\n\n"
)
# In-process circuit breaker: after N consecutive SDK failures in a single run,
# stop trying the SDK and serve the remainder of the run from the API.
_sdk_state = {"fails": 0, "off": False}
_SDK_MAX_FAILS = 3

# Which Claude calls may use Sonnet-via-SDK: all | exits | none.
# Default 'exits' — use Sonnet's stronger judgment on the crucial, low-volume
# EXIT decisions (selling = realizing P&L); the high-volume buy screen stays on
# Haiku. Set CLAUDE_SDK_FOR=all to route every Claude call through Sonnet.
CLAUDE_SDK_FOR = os.environ.get("CLAUDE_SDK_FOR", "exits").lower()

# Quota valve for the LLM exit analyst. Off by default. When on, positions
# trading at or above their MA5 skip the LLM review and are held. Measured on
# 19,654 historical reviews: -65% LLM calls, retains 73% of executed exits.
# Turn this on when subscription quota is the binding constraint, not before.
EXIT_GATE = os.environ.get("EXIT_GATE", "0") == "1"

# ── Per-call LLM cost logging ─────────────────────────────────────────────
#
# Anthropic API pricing, USD per MTok (million tokens). Verified against
# https://platform.claude.com/docs/en/about-claude/pricing on 2026-08-15.
# "cache_write_5m" is the rate for the ephemeral 5-minute cache_control block
# _claude_via_api sets on the system prompt (the only cache TTL this file
# uses); "cache_read" is a hit against that cache. Rates change over time —
# update this dict, and only this dict, when Anthropic republishes pricing.
CLAUDE_PRICES = {
    "claude-haiku-4-5-20251001": {
        "input":          1.00,   # $/MTok, base (uncached) input
        "output":         5.00,   # $/MTok
        "cache_write_5m": 1.25,   # $/MTok, 5-minute ephemeral cache write
        "cache_read":     0.10,   # $/MTok, cache hit
    },
}


def _claude_cost_usd(model: str, input_tokens, output_tokens,
                      cache_creation_input_tokens, cache_read_input_tokens):
    """USD cost of one metered call from its token counts, or None if `model`
    has no entry in CLAUDE_PRICES (unpriced rather than silently wrong)."""
    rates = CLAUDE_PRICES.get(model)
    if rates is None:
        return None
    return round(
        (input_tokens or 0)                  / 1_000_000 * rates["input"]
        + (output_tokens or 0)               / 1_000_000 * rates["output"]
        + (cache_creation_input_tokens or 0) / 1_000_000 * rates["cache_write_5m"]
        + (cache_read_input_tokens or 0)     / 1_000_000 * rates["cache_read"],
        6,
    )


def _log_llm_call(*, model: str, path: str, call_type: str, label: str,
                   latency_ms: float, input_tokens=None, output_tokens=None,
                   cache_creation_input_tokens=None, cache_read_input_tokens=None,
                   cost_usd=None, billing: str = "metered", error=None) -> None:
    """Append one record to llm_calls.jsonl. Fail-safe by design: a logging
    error must never break a trading run, so any failure here is swallowed
    (after a warning) rather than propagated to the caller."""
    try:
        jsonl_append(LOG_DIR / "llm_calls.jsonl", {
            "timestamp":                   now_et().isoformat(),
            "model":                       model,
            "path":                        path,          # "haiku-api" | "sonnet-sdk"
            "call_type":                   call_type,      # "buy" | "exit"
            "ticker":                      label,
            "latency_ms":                  round(latency_ms, 1),
            "input_tokens":                input_tokens,
            "output_tokens":               output_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "cache_read_input_tokens":     cache_read_input_tokens,
            "cost_usd":                    cost_usd,       # 0.0 for subscription calls
            "billing":                     billing,        # "metered" | "subscription"
            "error":                       error,
        })
    except Exception as exc:
        log.warning("llm_calls.jsonl logging failed (non-fatal): %s", exc)


def _sdk_allowed_for(call_type: str) -> bool:
    if CLAUDE_SDK_FOR == "all":
        return True
    if CLAUDE_SDK_FOR == "none":
        return False
    return call_type == "exit"


def _claude_use_sdk() -> bool:
    return (
        CLAUDE_SDK_ENABLED
        and bool(CLAUDE_OAUTH_TOKEN)
        and not _sdk_state["off"]
    )


def _claude_via_sdk(system: str, user: str, call_type: str = "exit", label: str = "") -> tuple:
    """One-shot Claude Sonnet call via the `claude` CLI (subscription auth).

    Runs from /tmp with ANTHROPIC_API_KEY stripped so the CLI uses the OAuth
    subscription token (your Pro plan's included usage) rather than metered API
    billing. Returns the response text; raises on any error so the caller can
    fall back to the Haiku API.
    """
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["CLAUDE_CODE_OAUTH_TOKEN"] = CLAUDE_OAUTH_TOKEN
    start = time.time()
    try:
        proc = subprocess.run(
            [CLAUDE_CLI_PATH, "-p", user,
             "--model", CLAUDE_SDK_MODEL,
             "--system-prompt", _SDK_HARDENING + system,
             "--output-format", "json",
             "--max-turns", "1",
             "--allowedTools", "",
             "--strict-mcp-config",
             "--no-session-persistence",
             "--permission-mode", "default"],
            capture_output=True, text=True, timeout=CLAUDE_SDK_TIMEOUT, cwd="/tmp", env=env,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude exit {proc.returncode}: {proc.stderr[-200:]}")
        payload = json.loads(proc.stdout)
        if payload.get("is_error") or payload.get("subtype") != "success":
            raise RuntimeError(f"claude error: {str(payload.get('result'))[:200]}")
        text = (payload.get("result") or "").strip()
        if not text:
            raise RuntimeError("claude empty result")
    except Exception as exc:
        # SDK calls bill the subscription, never metered $ — cost_usd stays
        # 0.0 even on failure so this can never be mistaken for API spend.
        _log_llm_call(
            model=CLAUDE_SDK_MODEL, path="sonnet-sdk", call_type=call_type, label=label,
            latency_ms=(time.time() - start) * 1000,
            cost_usd=0.0, billing="subscription", error=str(exc)[:300],
        )
        raise

    # The CLI's --output-format json result carries an aggregate `usage`
    # block when available; older/other CLI builds may omit it entirely, in
    # which case we log null token counts rather than guess.
    usage = payload.get("usage") or {}
    _log_llm_call(
        model=CLAUDE_SDK_MODEL, path="sonnet-sdk", call_type=call_type, label=label,
        latency_ms=(time.time() - start) * 1000,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
        cache_read_input_tokens=usage.get("cache_read_input_tokens"),
        cost_usd=0.0, billing="subscription",
    )
    return text


def _claude_via_api(system: str, user: str, max_tokens: int,
                     call_type: str = "exit", label: str = "") -> str:
    """Metered Anthropic API call on Haiku (fallback path)."""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    start = time.time()
    try:
        msg = client.messages.create(
            model=CLAUDE_API_MODEL,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:
        _log_llm_call(
            model=CLAUDE_API_MODEL, path="haiku-api", call_type=call_type, label=label,
            latency_ms=(time.time() - start) * 1000,
            billing="metered", error=str(exc)[:300],
        )
        raise

    usage = msg.usage
    cache_creation = getattr(usage, "cache_creation_input_tokens", None)
    cache_read     = getattr(usage, "cache_read_input_tokens", None)
    cost = _claude_cost_usd(CLAUDE_API_MODEL, usage.input_tokens, usage.output_tokens,
                             cache_creation, cache_read)
    _log_llm_call(
        model=CLAUDE_API_MODEL, path="haiku-api", call_type=call_type, label=label,
        latency_ms=(time.time() - start) * 1000,
        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        cache_creation_input_tokens=cache_creation, cache_read_input_tokens=cache_read,
        cost_usd=cost, billing="metered",
    )
    return msg.content[0].text.strip()


def claude_complete(system: str, user: str, max_tokens: int, label: str = "",
                    call_type: str = "exit") -> tuple:
    """Unified Claude call: Sonnet via SDK (subscription) → Haiku via API.

    call_type ('buy' | 'exit') + CLAUDE_SDK_FOR decide whether Sonnet is tried.
    Returns (text, path) where path is 'sonnet-sdk' or 'haiku-api'. SDK problems
    never raise (always fall back to the API); the API path may still raise and
    is handled by the caller's retry().
    """
    if _sdk_allowed_for(call_type) and _claude_use_sdk():
        try:
            text = _claude_via_sdk(system, user, call_type=call_type, label=label)
            _sdk_state["fails"] = 0
            return text, "sonnet-sdk"
        except Exception as exc:
            _sdk_state["fails"] += 1
            log.warning("%sClaude SDK failed (%s) — falling back to Haiku API",
                        f"[{label}] " if label else "", str(exc)[:160])
            if _sdk_state["fails"] >= _SDK_MAX_FAILS:
                _sdk_state["off"] = True
                log.warning("Claude SDK disabled for the rest of this run after %d failures",
                            _SDK_MAX_FAILS)
    return _claude_via_api(system, user, max_tokens, call_type=call_type, label=label), "haiku-api"


# ── Step 3: Risk Manager — Claude Haiku ──────────────────────────────────────

RISK_SYSTEM = (
    "You are a chief risk officer. You receive raw market data, an analyst's recommendation, "
    "and an independent sentiment assessment. Scrutinize for: hallucinated data points, "
    "logical leaps, news misinterpretation, excessive risk. "
    'Respond ONLY in JSON: {"verdict": "APPROVED" or "VETO", "justification": string}. '
    "You must provide specific, concrete reasons for a VETO -- vague concerns like "
    "'too risky' or 'insufficient data' are NOT valid vetoes. Only VETO if you can identify "
    "a specific factual error, hallucination, logical contradiction, or clearly misinterpreted "
    "news headline in the analyst's reasoning. "
    "Keep justification under 2 sentences max. Be specific but terse. "
    "Example VETO: 'Analyst cites Nokia 6G collaboration not present in provided headlines.' "
    "Example APPROVED: 'Technical analysis aligns with provided data. No hallucinations detected.'"
)


def run_risk_manager(data_block: str, analyst_rec: dict, hf_sentiment: dict) -> tuple:
    ticker = analyst_rec.get("ticker", "?")

    user_msg = (
        f"MARKET DATA:\n{data_block}\n\n"
        f"ANALYST RECOMMENDATION:\n{json.dumps(analyst_rec, indent=2)}\n\n"
        f"INDEPENDENT SENTIMENT ASSESSMENT:\n"
        f"  Sentiment : {hf_sentiment.get('sentiment', 'N/A')}\n"
        f"  Confidence: {hf_sentiment.get('confidence', 'N/A')}%\n"
        f"  Reasoning : {hf_sentiment.get('reasoning', '')}"
    )

    def call():
        raw, path = claude_complete(RISK_SYSTEM, user_msg, max_tokens=150, label=ticker, call_type="buy")
        log.debug("Risk raw (%s) for %s: %s", path, ticker, raw[:300])
        try:
            data = json.loads(strip_json_fences(raw))
        except json.JSONDecodeError as parse_err:
            # Log the full raw output so we can debug truncated responses
            log.warning(
                "Haiku JSON parse error for %s (retrying): %s | raw=%s",
                ticker, parse_err, raw[:400],
            )
            raise  # let retry() handle it
        v = data.get("verdict", "VETO").upper()
        j = data.get("justification", "")
        return (v if v in ("APPROVED", "VETO") else "VETO"), j

    try:
        # retries=1: one immediate retry on parse failure before giving up
        return retry(call, retries=1, delay=2.0)
    except Exception as exc:
        log.error("Haiku risk manager failed for %s after retries: %s", ticker, exc)
        return "VETO", f"Risk manager parse error: {exc}"


# ── Step 0b: AI Exit Analysis ────────────────────────────────────────────────

EXIT_ANALYST_SYSTEM = (
    "You are a portfolio manager reviewing an open long position. "
    "Given the entry context, current market conditions, and recent news, "
    "decide whether to keep holding, partially reduce, or fully exit the position. "
    "Respond ONLY in JSON: "
    '{"action": "HOLD" or "TRIM" or "EXIT", "confidence": 0-100, "reasoning": string}. '
    "HOLD = keep full position. TRIM = sell ~50% of shares. EXIT = close entire position. "
    "Only recommend TRIM or EXIT if you identify a clear, specific deterioration: "
    "negative earnings surprise, product recall, legal trouble, technical breakdown below "
    "key support, or a fundamental change in the investment thesis. "
    "CRITICAL: Do NOT recommend EXIT or TRIM based on vague concerns like 'risk' or "
    "'uncertainty'. When in doubt, HOLD."
)

EXIT_RISK_SYSTEM = (
    "You are a chief risk officer reviewing a proposed portfolio exit or trim. "
    "The portfolio manager recommends EXITING or TRIMMING a position. "
    "Verify: Is the exit/trim reasoning grounded in actual data provided? "
    "Are there hallucinated news events, wrong prices, or invented catalysts? "
    "Is the proposed action proportionate and logically justified? "
    'Respond ONLY in JSON: {"verdict": "APPROVED" or "VETO", "justification": string}. '
    "VETO only if you can point to a specific factual error or hallucination. "
    "If the reasoning is sound, APPROVE it. "
    "Keep justification under 2 sentences max. Be specific but terse. "
    "Example VETO: 'Analyst cites Nokia 6G collaboration not present in provided headlines.' "
    "Example APPROVED: 'Technical analysis aligns with provided data. No hallucinations detected.'"
)


GEMINI_CLI_PATH       = os.environ.get("GEMINI_CLI_PATH", "/usr/bin/gemini")
# CLI fallback chain. Pro and Flash use separate quota pools; when Pro
# exhausts the loop drops to Flash on the next iteration. Override via
# GEMINI_CLI_EXIT_MODELS env (comma-separated). Singular GEMINI_CLI_EXIT_MODEL
# is honoured for backward compat (becomes the head of the chain).
_default_cli_chain = "gemini-3.5-flash,gemini-3.1-pro-preview,gemini-3-pro-preview,gemini-3-flash-preview,gemini-2.5-flash"
_legacy_single = os.environ.get("GEMINI_CLI_EXIT_MODEL")
GEMINI_CLI_EXIT_MODELS = [
    m.strip() for m in os.environ.get("GEMINI_CLI_EXIT_MODELS", _legacy_single or _default_cli_chain).split(",")
    if m.strip()
]
GEMINI_CLI_EXIT_MODEL = GEMINI_CLI_EXIT_MODELS[0]  # legacy alias (head of chain)
GEMINI_CLI_TIMEOUT    = int(os.environ.get("GEMINI_CLI_TIMEOUT", "60"))
# Google retired the Gemini CLI for individual (free/Pro) tiers on 2026-06-18;
# it now returns IneligibleTierError. The exit analyst therefore uses the Gemini
# **API** by default. Set USE_GEMINI_EXIT_CLI=1 only with a paid-key-backed CLI.
USE_GEMINI_EXIT_CLI   = os.environ.get("USE_GEMINI_EXIT_CLI", "0").lower() in ("1", "true", "yes")

# Antigravity CLI (agy): routes Gemini prompts through the local Google sign-in
# **subscription** quota instead of the metered/free-tier Gemini API key,
# sidestepping the free tier's 20 requests/day per model. That quota is not a
# daily request allowance -- it meters compute and resets weekly; exhausting it
# returns "Individual quota reached ... Resets in 167h55m45s", i.e. a 7-day
# lockout with no intra-day refill on the AI Plus tier. When USE_AGY_GEMINI=1 the
# analyst and exit analyst try agy first and fall back to the Gemini API chain.
AGY_BIN        = os.environ.get("AGY_BIN", str(Path.home() / ".local" / "bin" / "agy"))
# Default is 3.6 Flash (High) as of 2026-08-30. A benchmark with computed ground
# truth found 3.6 and 3.7 Flash indistinguishable on this kind of work and both
# well ahead of 3.5, so the tie breaks on availability: on the Gemini free tier --
# where this bot lands whenever agy fails -- 3.6 answers every call while 3.7
# mostly returns "high demand", so primary and fallback now run the same model.
# If subscription quota gets tight, drop to "(Medium)" before changing generation;
# the effort tier costs more than the generation does.
AGY_MODEL      = os.environ.get("AGY_MODEL", "Gemini 3.6 Flash (High)")
AGY_TIMEOUT    = int(os.environ.get("AGY_TIMEOUT", "120"))
USE_AGY_GEMINI = os.environ.get("USE_AGY_GEMINI", "0").lower() in ("1", "true", "yes")


def call_gemini_cli(system_prompt: str, user_prompt: str, model):
    """Call the local gemini CLI in headless mode.

    `model` may be a string (single attempt) or a list/tuple (fallback chain).
    Walks the chain on quota/empty/parse error and returns the first success.
    Pro models share one quota pool; Flash models share another, so when
    Pro exhausts the loop drops to Flash automatically.

    Returns:
        tuple: (response_text, used_model)  -- used_model is the model that
        actually produced the response (router-picked when available).
    Raises:
        RuntimeError: when every model in the chain fails.
    """
    full_prompt = f"{system_prompt}\n\n{user_prompt}"
    chain = [model] if isinstance(model, str) else list(model)
    last_exc = None
    for m in chain:
        try:
            proc = subprocess.run(
                [GEMINI_CLI_PATH, "-m", m, "-o", "json", "--approval-mode", "plan", "-p", full_prompt],
                capture_output=True,
                text=True,
                timeout=GEMINI_CLI_TIMEOUT,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"exit {proc.returncode}: {proc.stderr[-160:]}")
            payload = json.loads(proc.stdout)
            response_text = payload.get("response", "")
            if not response_text:
                raise RuntimeError("empty response field")
            actual_models = list(payload.get("stats", {}).get("models", {}).keys())
            used = actual_models[0] if actual_models else m
            log.debug("gemini CLI router used: %s", used)
            return response_text, used
        except Exception as exc:
            log.warning("gemini CLI model %s failed (%s) — trying next", m, str(exc)[:120])
            last_exc = exc
            continue
    raise RuntimeError(f"All CLI models failed: {last_exc}")


def call_agy(system_prompt: str, user_prompt: str) -> str:
    """Run a prompt through the Antigravity CLI (agy) in headless print mode.

    Uses the local Google sign-in subscription quota rather than metered Gemini
    API tokens. `agy -p` prints the model's reply to stdout; with a JSON-only
    system prompt it returns parseable JSON directly.

    Returns:
        str: raw stdout (expected JSON per the system prompt).
    Raises:
        RuntimeError: on non-zero exit or empty output.
    """
    full_prompt = f"{system_prompt}\n\n{user_prompt}"
    proc = subprocess.run(
        [AGY_BIN, "--model", AGY_MODEL, "-p", full_prompt],
        capture_output=True,
        text=True,
        timeout=AGY_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"agy exit {proc.returncode}: {proc.stderr[-160:]}")
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError("agy empty response")
    return out


def run_exit_analyst(position_context: str, batch_label: str = "") -> dict:
    """Call Gemini to decide HOLD / TRIM / EXIT for one position.

    Primary path: Gemini API with the GEMINI_MODEL_PRIORITY chain.
    Optional path: gemini CLI (USE_GEMINI_EXIT_CLI=1) — off by default because
    Google retired the CLI for individual tiers on 2026-06-18.

    position_context is the full text block (position stats + market data + buy reasoning).
    Returns dict with action, confidence, reasoning, model.
    """
    pfx = f"[{batch_label}] " if batch_label else ""

    # ── Optional agy path (subscription quota) — falls back to CLI/API below ──
    if USE_AGY_GEMINI:
        try:
            raw    = call_agy(EXIT_ANALYST_SYSTEM, position_context)
            data   = json.loads(strip_json_fences(raw))
            action = data.get("action", "HOLD").upper()
            if action not in ("HOLD", "TRIM", "EXIT"):
                action = "HOLD"
            log.info("%sExit analyst using agy: %s", pfx, AGY_MODEL)
            return {
                "action":     action,
                "confidence": int(data.get("confidence", 0)),
                "reasoning":  data.get("reasoning", ""),
                "model":      f"agy:{AGY_MODEL}",
            }
        except Exception as exc:
            log.warning("%sagy exit analyst failed (%s) — falling back…",
                        pfx, str(exc)[:120])

    # ── Optional CLI path (off by default; see USE_GEMINI_EXIT_CLI). The Gemini
    #    API chain below is the primary path. ──
    if USE_GEMINI_EXIT_CLI:
        try:
            raw, used_model = call_gemini_cli(EXIT_ANALYST_SYSTEM, position_context, GEMINI_CLI_EXIT_MODELS)
            data = json.loads(strip_json_fences(raw))
            action = data.get("action", "HOLD").upper()
            if action not in ("HOLD", "TRIM", "EXIT"):
                action = "HOLD"
            log.info("%sExit analyst using CLI model: %s", pfx, used_model)
            return {
                "action":     action,
                "confidence": int(data.get("confidence", 0)),
                "reasoning":  data.get("reasoning", ""),
                "model":      f"cli:{used_model}",
            }
        except Exception as exc:
            log.warning("%sCLI exit analyst failed (%s) — falling back to API…",
                        pfx, str(exc)[:120])

    # ── Primary: Gemini API priority chain ──
    client  = genai.Client(api_key=GEMINI_API_KEY)
    last_exc = None

    for model in GEMINI_MODEL_PRIORITY:
        try:
            def call(m=model):
                response = client.models.generate_content(
                    model=m,
                    contents=position_context,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=EXIT_ANALYST_SYSTEM,
                        response_mime_type="application/json",
                    ),
                )
                return response.text

            raw  = retry(call, retries=1, delay=4.0)
            data = json.loads(strip_json_fences(raw))
            action = data.get("action", "HOLD").upper()
            if action not in ("HOLD", "TRIM", "EXIT"):
                action = "HOLD"
            log.info("%sExit analyst using model: %s", pfx, model)
            return {
                "action":     action,
                "confidence": int(data.get("confidence", 0)),
                "reasoning":  data.get("reasoning", ""),
                "model":      model,
            }

        except Exception as exc:
            log.warning("%sExit analyst model %s failed (%s) -- trying next…", pfx, model, str(exc)[:70])
            last_exc = exc
            continue

    log.error("%sAll Gemini models failed for exit analysis: %s", pfx, last_exc)
    return {"action": "HOLD", "confidence": 0, "reasoning": f"Gemini error: {last_exc}", "model": "none"}


def run_exit_risk_manager(position_context: str, exit_rec: dict) -> tuple:
    """Haiku gate for EXIT / TRIM decisions.

    Returns (verdict, justification) where verdict is APPROVED or VETO.
    """
    ticker = exit_rec.get("ticker", "?")

    user_msg = (
        f"POSITION & MARKET DATA:\n{position_context}\n\n"
        f"PORTFOLIO MANAGER RECOMMENDATION:\n"
        f"  Action     : {exit_rec.get('action')}\n"
        f"  Confidence : {exit_rec.get('confidence')}%\n"
        f"  Reasoning  : {exit_rec.get('reasoning', '')}"
    )

    def call():
        raw, path = claude_complete(EXIT_RISK_SYSTEM, user_msg, max_tokens=100, label=ticker, call_type="exit")
        log.debug("Exit risk raw (%s) for %s: %s", path, ticker, raw[:300])
        try:
            data = json.loads(strip_json_fences(raw))
        except json.JSONDecodeError as parse_err:
            log.warning("Exit Haiku JSON parse error for %s (retrying): %s | raw=%s",
                        ticker, parse_err, raw[:400])
            raise
        v = data.get("verdict", "VETO").upper()
        j = data.get("justification", "")
        return (v if v in ("APPROVED", "VETO") else "VETO"), j

    try:
        return retry(call, retries=1, delay=2.0)
    except Exception as exc:
        log.error("Exit Haiku failed for %s after retries: %s", ticker, exc)
        return "VETO", f"Exit risk manager error: {exc}"


# ── Step 4: Trade Execution ───────────────────────────────────────────────────

def calc_sim_fees(action: str, qty: int, price: float) -> dict:
    """Simulate realistic US-equity trading fees (Alpaca Live pricing model).

    Buys: no regulatory fees, slippage only.
    Sells: SEC fee + FINRA TAF + slippage.
    Slippage applies to both sides (0.05% of notional).
    """
    notional    = qty * price
    slippage    = round(notional * 0.0005, 4)
    if action == "SELL":
        sec_fee   = round(notional * 0.0000278, 4)
        finra_taf = round(min(qty * 0.000166, 8.30), 4)
    else:
        sec_fee   = 0.0
        finra_taf = 0.0
    total = round(sec_fee + finra_taf + slippage, 4)
    return {
        "sim_sec_fee":   sec_fee,
        "sim_finra_taf": finra_taf,
        "sim_slippage":  slippage,
        "sim_total_fee": total,
    }


def open_order_symbols(client: TradingClient) -> set:
    """Symbols that already have a still-open (unfilled) order. Used to skip
    re-submitting across cron runs while a prior order is pending — otherwise a
    second SELL could double-sell into an unintended short, or a second BUY
    could double the position."""
    try:
        orders = client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
        return {o.symbol for o in orders}
    except Exception as exc:
        log.warning("Could not fetch open orders (%s) — order-dedup guard disabled this run", exc)
        return set()


def _client_order_id(tag: str, ticker: str) -> str:
    """Unique, broker-safe client_order_id for idempotency + traceability."""
    return f"{tag}-{ticker}-{now_et().strftime('%Y%m%d%H%M%S')}"[:128]


def execute_trades(approved: list, data_map: dict, client: TradingClient) -> dict:
    """Submit approved orders. Returns {ticker: status} so the caller can label
    decisions.jsonl with what actually happened (EXECUTED vs a SKIP reason vs
    ORDER_FAILED) instead of blanket-stamping every approved rec as _EXECUTED.
    """
    account       = client.get_account()
    portfolio_val = float(account.portfolio_value)
    max_trade_val = portfolio_val * MAX_POSITION_PCT

    positions  = {p.symbol: p for p in client.get_all_positions()}
    open_count = len(positions)
    pending    = open_order_symbols(client)   # tickers with an unfilled order
    ts         = now_et().isoformat()

    status: dict = {}
    bought: set = set()   # guards against duplicate analyst recs double-buying

    for rec in approved:
        ticker = rec["ticker"]
        action = rec["recommendation"]
        price  = data_map[ticker]["current_price"]

        if action == "BUY":
            if open_count >= MAX_POSITIONS:
                log.info("SKIP %s BUY: max open positions (%d) reached", ticker, MAX_POSITIONS)
                status[ticker] = "SKIPPED_MAX_POS"
                continue
            if ticker in positions or ticker in bought or ticker in pending:
                log.info("SKIP %s BUY: position open / already bought / order pending", ticker)
                status[ticker] = "SKIPPED_OPEN"
                continue

            qty = int(max_trade_val / price)
            if qty < 1:
                log.info("SKIP %s BUY: one share ($%.2f) exceeds position budget ($%.2f)",
                         ticker, price, max_trade_val)
                status[ticker] = "SKIPPED_BUDGET"
                continue
            try:
                order = client.submit_order(MarketOrderRequest(
                    symbol=ticker,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=_client_order_id("buy", ticker),
                ))
                fees = calc_sim_fees("BUY", qty, price)
                jsonl_append(LOG_DIR / "executed.jsonl", {
                    "timestamp":  ts,
                    "action":     "BUY",
                    "ticker":     ticker,
                    "qty":        qty,
                    "price":      price,
                    "order_id":   str(order.id),
                    "confidence": rec.get("confidence"),
                    "reasoning":  rec.get("reasoning", ""),
                    **fees,
                })
                log.info("BUY  %dx %s @ ~$%.2f | sim_fee=$%.4f | order %s",
                         qty, ticker, price, fees["sim_total_fee"], order.id)
                open_count += 1
                bought.add(ticker)
                status[ticker] = "EXECUTED"
            except Exception as exc:
                log.error("BUY order failed for %s: %s", ticker, exc)
                status[ticker] = "ORDER_FAILED"

        elif action == "SELL":
            if ticker not in positions:
                log.info("SKIP %s SELL: no position held", ticker)
                status[ticker] = "SKIPPED_NO_POS"
                continue
            if ticker in pending:
                log.info("SKIP %s SELL: an order is already pending", ticker)
                status[ticker] = "SKIPPED_PENDING"
                continue

            held_qty = int(float(positions[ticker].qty))
            try:
                order = client.submit_order(MarketOrderRequest(
                    symbol=ticker,
                    qty=held_qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=_client_order_id("sell", ticker),
                ))
                fees = calc_sim_fees("SELL", held_qty, price)
                jsonl_append(LOG_DIR / "executed.jsonl", {
                    "timestamp":  ts,
                    "action":     "SELL",
                    "ticker":     ticker,
                    "qty":        held_qty,
                    "price":      price,
                    "order_id":   str(order.id),
                    "confidence": rec.get("confidence"),
                    "reasoning":  rec.get("reasoning", ""),
                    **fees,
                })
                log.info("SELL %dx %s @ ~$%.2f | sim_fee=$%.4f | order %s",
                         held_qty, ticker, price, fees["sim_total_fee"], order.id)
                open_count -= 1
                status[ticker] = "EXECUTED"
            except Exception as exc:
                log.error("SELL order failed for %s: %s", ticker, exc)
                status[ticker] = "ORDER_FAILED"

    return status


# ── Portfolio Snapshot (for dashboard chart) ──────────────────────────────────

def snapshot_portfolio(client: TradingClient, run_ts: str) -> None:
    try:
        account   = client.get_account()
        positions = client.get_all_positions()
        jsonl_append(LOG_DIR / "portfolio_history.jsonl", {
            "timestamp":            run_ts,
            "portfolio_value":      float(account.portfolio_value),
            "cash":                 float(account.cash),
            "equity":               float(account.equity),
            "open_positions_count": len(positions),
        })
    except Exception as exc:
        log.error("Portfolio snapshot failed: %s", exc)


# ── Portfolio Summary ─────────────────────────────────────────────────────────

def print_portfolio_summary(client: TradingClient) -> None:
    account   = client.get_account()
    positions = client.get_all_positions()

    equity   = float(account.equity)
    cash     = float(account.cash)
    port_val = float(account.portfolio_value)
    day_pnl  = equity - float(account.last_equity)

    sep = "=" * 54
    print(f"\n{sep}")
    print(f"  PORTFOLIO SUMMARY  —  {now_et().strftime('%Y-%m-%d %H:%M ET')}")
    print(sep)
    print(f"  Equity        : ${equity:>13,.2f}")
    print(f"  Cash          : ${cash:>13,.2f}")
    print(f"  Portfolio Val : ${port_val:>13,.2f}")
    print(f"  Day P&L       : ${day_pnl:>+13,.2f}")
    print(f"\n  Open Positions ({len(positions)}/{MAX_POSITIONS}):")
    if positions:
        for p in positions:
            unr_pl  = float(p.unrealized_pl)
            unr_pct = float(p.unrealized_plpc) * 100
            print(f"    {p.symbol:<6}  {p.qty:>6} shares  "
                  f"unreal P&L: ${unr_pl:>+9,.2f}  ({unr_pct:>+.2f}%)")
    else:
        print("    (none)")
    print(f"{sep}\n")


# ── Step 0a: Hard stop-loss (math only, no AI) ───────────────────────────────

def check_hard_stops(alpaca: TradingClient, run_ts: str) -> list:
    """Immediately sell any position that breaches the hard floor OR a trailing stop.

    Two AI-independent safety rules, evaluated before anything else:
      • HARD FLOOR — unrealised loss >= STOP_LOSS_PCT from entry.
      • TRAILING   — once a position's running peak is >= TRAIL_ACTIVATE_PCT above
                     entry, exit on a >= TRAIL_STOP_PCT pullback from that peak
                     (locks in profit on winners).
    Running peaks persist in PEAKS_FILE and are sampled each run (cron cadence).
    Returns list of tickers that were market-sold.
    """
    try:
        positions = alpaca.get_all_positions()
    except Exception as exc:
        # FAIL CLOSED: without positions we cannot evaluate stops, so we must not
        # let the run proceed to open NEW positions on un-risk-checked holdings.
        # Abort the whole run; the next cron cycle retries.
        log.critical("Could not fetch positions for stop-loss check: %s — aborting run.", exc)
        raise

    peaks   = load_peaks()
    pending = open_order_symbols(alpaca)   # don't re-submit while a stop is unfilled
    sold_tickers: list = []

    for p in positions:
        ticker    = p.symbol
        current   = float(p.current_price)
        entry     = float(p.avg_entry_price)
        plpc      = float(p.unrealized_plpc) * 100
        qty       = int(float(p.qty))

        # Update the running high-water mark since entry.
        peak = max(peaks.get(ticker, entry), current)
        peaks[ticker] = round(peak, 4)

        gain_from_entry = (peak - entry) / entry * 100 if entry else 0.0
        drawdown        = (peak - current) / peak * 100 if peak else 0.0
        trailing_armed  = gain_from_entry >= TRAIL_ACTIVATE_PCT

        hit_hard     = plpc <= -STOP_LOSS_PCT
        hit_trailing = trailing_armed and drawdown >= TRAIL_STOP_PCT
        if not (hit_hard or hit_trailing):
            continue
        if ticker in pending:
            log.info("  %-6s stop triggered but an order is already pending — skipping", ticker)
            continue

        if hit_hard:
            exit_type = "STOP-LOSS"
            reason = f"HARD STOP-LOSS at {plpc:.2f}% (threshold: -{STOP_LOSS_PCT}%)"
        else:
            exit_type = "TRAILING-STOP"
            reason = (f"TRAILING STOP: -{drawdown:.2f}% from peak ${peak:.2f} "
                      f"(armed +{gain_from_entry:.2f}%, threshold -{TRAIL_STOP_PCT}%)")
        log.warning("  %-6s  %s", ticker, reason)
        try:
            order = alpaca.submit_order(MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                client_order_id=_client_order_id("stop", ticker),
            ))
            fees = calc_sim_fees("SELL", qty, current)
            jsonl_append(LOG_DIR / "executed.jsonl", {
                "timestamp": run_ts,
                "action":    "SELL",
                "ticker":    ticker,
                "qty":       qty,
                "price":     current,
                "order_id":  str(order.id),
                "exit_type": exit_type,
                "reason":    reason,
                **fees,
            })
            log.info("  %s %dx %s @ ~$%.2f | order %s",
                     exit_type, qty, ticker, current, order.id)
            sold_tickers.append(ticker)
        except Exception as exc:
            log.error("  Stop-loss order failed for %s: %s", ticker, exc)

    # Drop peaks for tickers we no longer hold (closed or just sold) so the file
    # stays bounded and a later re-buy starts a fresh high-water mark.
    held  = {p.symbol for p in positions} - set(sold_tickers)
    peaks = {t: v for t, v in peaks.items() if t in held}
    save_peaks(peaks)

    return sold_tickers


# ── Step 0b: AI exit analysis ─────────────────────────────────────────────────

def run_ai_exits(
    alpaca: TradingClient,
    data_map: dict,
    headlines_map: dict,
    run_ts: str,
    api_calls: dict,
    hard_stopped: set | None = None,
) -> tuple:
    """Run Gemini + Haiku exit analysis for all open positions not already stopped out.

    Returns:
      (sold_tickers: list[str], exit_decisions: list[dict])
      exit_decisions contains one record per position reviewed (for decisions.jsonl).
    """
    try:
        positions = alpaca.get_all_positions()
    except Exception as exc:
        log.error("Could not fetch positions for AI exit check: %s", exc)
        return [], []

    if not positions:
        return [], []

    pending = open_order_symbols(alpaca)
    sold_tickers:   list = []
    exit_decisions: list = []
    hard_stopped = hard_stopped or set()

    for p in positions:
        ticker    = p.symbol
        # Skip names already hard-stopped this run: their stop SELL may still be
        # unfilled (halt/open volatility), so a second AI-driven SELL here would
        # double-sell — rejected at best, an unintended short at worst.
        if ticker in hard_stopped:
            log.info("  %-6s skipping AI exit — already hard-stopped this run", ticker)
            continue
        if ticker in pending:
            log.info("  %-6s skipping AI exit — an order is already pending", ticker)
            continue
        current   = float(p.current_price)
        avg_entry = float(p.avg_entry_price)
        plpc      = float(p.unrealized_plpc) * 100
        qty       = int(float(p.qty))

        entry_dt  = find_entry_date(ticker)
        days_held = trading_days_between(entry_dt, now_et()) if entry_dt else "unknown"

        # Build position context block
        mdata = data_map.get(ticker)
        if mdata is None:
            log.warning("  %-6s no market data for AI exit review — skipping", ticker)
            continue

        market_block     = build_data_block(mdata, headlines_map.get(ticker, []))
        original_reason  = find_original_buy_reasoning(ticker)

        position_context = (
            f"OPEN POSITION: {ticker}\n"
            f"  Entry Price        : ${avg_entry:.2f}\n"
            f"  Current Price      : ${current:.2f}\n"
            f"  Unrealized P&L     : {plpc:+.2f}%\n"
            f"  Qty Held           : {qty}\n"
            f"  Days Held          : {days_held}\n\n"
            f"{market_block}\n\n"
            f"ORIGINAL BUY REASONING:\n"
            f"  {original_reason or '(not found)'}"
        )

        log.info("  %-6s  P&L=%+.2f%%  days=%s — running AI exit review…",
                 ticker, plpc, days_held)

        # Quota valve (EXIT_GATE=1): skip the LLM for positions that are not
        # near a decision boundary. Fails open when market data is unusable.
        if EXIT_GATE:
            gated = False
            try:
                gated = float(mdata["current_price"]) >= float(mdata["ma5"])
            except (KeyError, TypeError, ValueError):
                gated = False
            if gated:
                log.info("  %-6s  exit gate: at/above MA5 — LLM review skipped", ticker)
                exit_decisions.append({
                    "decision_type":          "exit_review",
                    "ticker":                 ticker,
                    "entry_price":            avg_entry,
                    "current_price":          current,
                    "unrealized_plpc":        round(plpc, 2),
                    "days_held":              days_held,
                    "gemini_exit_action":     "HOLD",
                    "gemini_exit_confidence": 0,
                    "gemini_exit_reasoning":  "gated: price at/above MA5, no LLM review",
                    "gemini_exit_model":      "gate",
                    "position_context":       position_context,
                    "haiku_exit_verdict":     "N/A",
                    "haiku_exit_justification": "",
                    "final_exit_action":      "HELD",
                })
                continue

        # Gemini exit analyst
        exit_rec = run_exit_analyst(position_context, batch_label=ticker)
        api_calls["exit_gemini"] = api_calls.get("exit_gemini", 0) + 1

        exit_action     = exit_rec["action"]           # HOLD / TRIM / EXIT
        exit_confidence = exit_rec["confidence"]
        exit_reasoning  = exit_rec["reasoning"]

        log.info("  %-6s  Gemini exit=%s (%d%%) — %s",
                 ticker, exit_action, exit_confidence, exit_reasoning[:80])

        if exit_action == "HOLD":
            # No Haiku needed — keep position
            exit_decisions.append({
                "decision_type":          "exit_review",
                "ticker":                 ticker,
                "entry_price":            avg_entry,
                "current_price":          current,
                "unrealized_plpc":        round(plpc, 2),
                "days_held":              days_held,
                "gemini_exit_action":     exit_action,
                "gemini_exit_confidence": exit_confidence,
                "gemini_exit_reasoning":  exit_reasoning,
                "gemini_exit_model":      exit_rec.get("model", ""),
                "position_context":       position_context,
                "haiku_exit_verdict":     "N/A",
                "haiku_exit_justification": "",
                "final_exit_action":      "HELD",
            })
            continue

        # EXIT or TRIM → Haiku gate
        exit_rec["ticker"] = ticker    # needed inside run_exit_risk_manager
        haiku_verdict, haiku_just = run_exit_risk_manager(position_context, exit_rec)
        api_calls["exit_haiku"] = api_calls.get("exit_haiku", 0) + 1

        log.info("  %-6s  Haiku exit verdict: %s", ticker, haiku_verdict)

        base_dec = {
            "decision_type":            "exit_review",
            "ticker":                   ticker,
            "entry_price":              avg_entry,
            "current_price":            current,
            "unrealized_plpc":          round(plpc, 2),
            "days_held":                days_held,
            "gemini_exit_action":       exit_action,
            "gemini_exit_confidence":   exit_confidence,
            "gemini_exit_reasoning":    exit_reasoning,
            "gemini_exit_model":        exit_rec.get("model", ""),
            "position_context":         position_context,
            "haiku_exit_verdict":       haiku_verdict,
            "haiku_exit_justification": haiku_just,
        }

        if haiku_verdict != "APPROVED":
            log.info("  %-6s  Exit VETOED by Haiku: %s", ticker, haiku_just[:80])
            exit_decisions.append({**base_dec, "final_exit_action": "VETOED"})
            continue

        # Approved — execute
        if exit_action == "TRIM":
            sell_qty = max(1, qty // 2)
            exit_type_label = "AI-TRIM"
        else:
            sell_qty = qty
            exit_type_label = "AI-EXIT"

        try:
            order = alpaca.submit_order(MarketOrderRequest(
                symbol=ticker,
                qty=sell_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                client_order_id=_client_order_id("aiexit", ticker),
            ))
            fees = calc_sim_fees("SELL", sell_qty, current)
            jsonl_append(LOG_DIR / "executed.jsonl", {
                "timestamp": run_ts,
                "action":    "SELL",
                "ticker":    ticker,
                "qty":       sell_qty,
                "price":     current,
                "order_id":  str(order.id),
                "exit_type": exit_type_label,
                "reason":    exit_reasoning[:200],
                **fees,
            })
            log.info("  %s  %dx %s @ ~$%.2f | sim_fee=$%.4f | order %s",
                     exit_type_label, sell_qty, ticker, current,
                     fees["sim_total_fee"], order.id)
            sold_tickers.append(ticker)
            final_action = "TRIMMED" if exit_action == "TRIM" else "EXITED"
        except Exception as exc:
            log.error("  AI exit order failed for %s: %s", ticker, exc)
            final_action = "ORDER_FAILED"

        exit_decisions.append({**base_dec, "final_exit_action": final_action})

    return sold_tickers, exit_decisions


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Single-instance lock: a slow run (CLI timeouts, retry sleeps, 50-ticker
    # ingestion) can exceed the 30-min cron interval. Two overlapping instances
    # would each see "no position" for a ticker and both BUY (2x size), and
    # interleave jsonl writes. flock guarantees only one runs at a time.
    # Imported lazily so the module still parses on non-Linux dev machines.
    import fcntl
    _lock_fh = open(LOG_DIR / "trader.lock", "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("Another trader run is still in progress — exiting to avoid overlap.")
        sys.exit(0)

    alpaca = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)

    if not is_market_open(alpaca):
        log.info("Market closed at %s — exiting.", now_et().isoformat())
        jsonl_append(LOG_DIR / "run_log.jsonl", {
            "timestamp": now_et().isoformat(),
            "event":     "market_closed",
        })
        sys.exit(0)

    run_ts = now_et().isoformat()
    log.info("=== Trader run starting %s (%d tickers, batch=%d, max_pos=%d, pos_pct=%.0f%%) ===",
             run_ts, len(TICKERS), TICKER_BATCH_SIZE, MAX_POSITIONS, MAX_POSITION_PCT * 100)

    # API call counters for run_log
    api_calls = {"gemini": 0, "hf": 0, "haiku": 0, "exit_gemini": 0, "exit_haiku": 0}

    # ── Step 0a: Hard stop-loss (pure math, no AI) ────────────────────────
    log.info("Step 0a: Hard stop-loss check (≥%.0f%% loss)…", STOP_LOSS_PCT)
    hard_stopped = check_hard_stops(alpaca, run_ts)
    if hard_stopped:
        log.info("  Hard-stopped %d position(s): %s", len(hard_stopped), ", ".join(hard_stopped))
    else:
        log.info("  No stop-losses triggered.")

    # ── Step 1: Data Ingestion (with 1 s rate-limit between news calls) ──
    log.info("Step 1: Fetching market data and news for %d tickers…", len(TICKERS))
    data_map:      dict = {}
    headlines_map: dict = {}
    ticker_batch:  dict = {}   # ticker → batch label (for logging)

    for ticker in TICKERS:
        try:
            mdata = retry(lambda t=ticker: fetch_market_data(t))
        except Exception as exc:
            log.warning("  Skipping %s — market data failed: %s", ticker, exc)
            continue
        # Headlines are optional (build_data_block handles []). A news-API
        # outage must NOT drop a ticker whose prices fetched fine, nor abort
        # the whole run — degrade to technicals-only analysis instead.
        try:
            headlines = retry(lambda t=ticker: fetch_news(t, rate_limit_sleep=1.0))
        except Exception as exc:
            log.warning("  %-6s news fetch failed (%s) — proceeding technicals-only", ticker, exc)
            headlines = []
        data_map[ticker]      = mdata
        headlines_map[ticker] = headlines
        log.info("  %-6s $%.2f  (%+.2f%%)", ticker, mdata["current_price"], mdata["daily_change_pct"])

    if not data_map:
        log.error("No market data retrieved. Aborting run.")
        sys.exit(1)

    # Build ordered data_blocks; assign batch labels
    ingested_tickers = list(data_map.keys())
    ordered_blocks: list[tuple[str, str]] = []   # (ticker, data_block)
    for ticker in ingested_tickers:
        ordered_blocks.append((
            ticker,
            build_data_block(data_map[ticker], headlines_map.get(ticker, [])),
        ))

    # ── Step 0b: AI exit analysis for positions not already stopped ──────
    log.info("Step 0b: AI exit analysis for open positions…")
    ai_exited, exit_decisions = run_ai_exits(
        alpaca, data_map, headlines_map, run_ts, api_calls, set(hard_stopped)
    )
    if ai_exited:
        log.info("  AI-exited/trimmed %d position(s): %s", len(ai_exited), ", ".join(ai_exited))
    else:
        log.info("  No AI exits this cycle.")

    # ── Step 2: Analyst — batched Gemini calls (TICKER_BATCH_SIZE per call) ──
    log.info("Step 2: Analyst — %d tickers in batches of %d…",
             len(ingested_tickers), TICKER_BATCH_SIZE)

    analyst_recs: list = []
    gemini_model_used = "unknown"

    for batch_start in range(0, len(ordered_blocks), TICKER_BATCH_SIZE):
        batch_items  = ordered_blocks[batch_start: batch_start + TICKER_BATCH_SIZE]
        batch_tickers = [t for t, _ in batch_items]
        batch_blocks  = [b for _, b in batch_items]
        batch_num     = batch_start // TICKER_BATCH_SIZE + 1
        batch_label   = f"batch {batch_num} [{', '.join(batch_tickers)}]"

        # Track which batch each ticker belongs to
        for t in batch_tickers:
            ticker_batch[t] = batch_num

        log.info("  %s — calling Gemini…", batch_label)
        try:
            # run_analyst already walks the full model-priority chain (each
            # model retried once), so an outer retry here just doubled every
            # attempt — up to 28 calls + minutes of sleep per batch on outage.
            batch_recs, model = run_analyst(batch_blocks, batch_label)
            analyst_recs.extend(batch_recs)
            gemini_model_used = model       # last-used model (usually consistent)
            api_calls["gemini"] += 1
        except Exception as exc:
            log.error("  Gemini failed for %s: %s — skipping batch", batch_label, exc)
            # Inject HOLD fallbacks so tickers aren't silently missing from decisions
            for t in batch_tickers:
                analyst_recs.append({
                    "ticker":         t,
                    "recommendation": "HOLD",
                    "confidence":     0,
                    "reasoning":      f"Gemini batch error: {exc}",
                })

    # Index recs by ticker
    recs_by_ticker = {r.get("ticker", ""): r for r in analyst_recs}

    buy_sell_count = sum(1 for r in analyst_recs if r.get("recommendation", "HOLD") != "HOLD")
    log.info("  Gemini summary: %d total recs, %d BUY/SELL, %d HOLD",
             len(analyst_recs), buy_sell_count, len(analyst_recs) - buy_sell_count)
    for r in analyst_recs:
        log.info("  %-6s → %-4s  (conf=%s, batch=%s)",
                 r.get("ticker"), r.get("recommendation"),
                 r.get("confidence"), ticker_batch.get(r.get("ticker", ""), "?"))

    # ── Step 3: HF Sentiment + Haiku risk review (BUY/SELL only) ────────
    log.info("Step 3: HF Sentiment + Haiku risk review (BUY/SELL only)…")
    approved_trades: list = []
    vetoed_count    = 0
    decisions: dict = {}

    # Snapshot of slot/position state as of the start of this run, used by the
    # pre-Haiku gate below to skip the metered Claude call for candidates that
    # cannot possibly be executed (Step 4 would reject them with SKIPPED_MAX_POS
    # / SKIPPED_OPEN / SKIPPED_NO_POS regardless of what Haiku says). This is a
    # *separate* fetch from the one execute_trades makes for the real order
    # submission — that one stays authoritative; this one only decides whether
    # to spend a Haiku call. If the fetch fails, disable the pre-check for this
    # run rather than guess, so we fall back to today's exact behaviour.
    try:
        run_start_positions = {p.symbol: p for p in alpaca.get_all_positions()}
        run_start_pending   = open_order_symbols(alpaca)
        pre_check_enabled   = True
    except Exception as exc:
        log.warning("Pre-Haiku gate disabled this run — position snapshot failed: %s", exc)
        run_start_positions = {}
        run_start_pending   = set()
        pre_check_enabled   = False
    # Running count of positions that will be open once every BUY approved so
    # far *in this run* executes — starts at the pre-run count and increments
    # each time a candidate clears the gate (mirrors execute_trades' own
    # open_count, just computed ahead of time). Must be a running count, not a
    # stale snapshot: a slot consumed by an earlier candidate in this same run
    # has to reduce room for a later one.
    running_open_count = len(run_start_positions)

    NEWS_REFERENCE_KEYWORDS = (
        "report", "announce", "launch", "release", "beat", "miss", "surge",
        "plunge", "jump", "drop", "fell", "rose", "gain", "loss", "revenue",
        "earnings", "profit", "quarter", "deal", "acqui", "partner", "invest",
        "product", "recall", "lawsuit", "regulat", "news", "headline",
    )

    for rec in analyst_recs:
        ticker            = rec.get("ticker", "")
        # Normalize the analyst verdict: a variant like "Buy"/"strong buy"
        # would otherwise be != "HOLD", burn HF+Haiku calls, then match no
        # execution branch while still being stamped "<x>_EXECUTED".
        action = str(rec.get("recommendation", "HOLD")).strip().upper()
        if action not in ("BUY", "SELL", "HOLD"):
            action = "HOLD"
        rec["recommendation"] = action
        gemini_confidence = safe_int(rec.get("confidence"))

        # ── HOLDs: skip HF + Haiku entirely ──────────────────────────────
        if action == "HOLD":
            log.info("  %-6s HOLD — skipping review", ticker)
            decisions[ticker] = {
                "haiku_verdict": "N/A", "haiku_justification": "",
                "hf_sentiment": "N/A", "hf_confidence": None,
                "hf_reasoning": "", "hf_model": "",
                "final_action": "HOLD",
                "gemini_batch": ticker_batch.get(ticker),
            }
            continue

        if ticker not in data_map:
            log.warning("  %-6s no market data — skipping", ticker)
            decisions[ticker] = {
                "haiku_verdict": "N/A", "haiku_justification": "",
                "hf_sentiment": "N/A", "hf_confidence": None,
                "hf_reasoning": "", "hf_model": "",
                "final_action": "SKIPPED",
                "gemini_batch": ticker_batch.get(ticker),
            }
            continue

        # 3a. HF Sentiment
        try:
            hf = retry(lambda t=ticker: run_sentiment_analyst(
                build_data_block(data_map[t], headlines_map.get(t, []))
            ), retries=1, delay=3.0)
            api_calls["hf"] += 1
        except Exception as exc:
            log.warning("  %-6s HF error: %s", ticker, exc)
            hf = {"sentiment": "NEUTRAL", "confidence": 0,
                  "reasoning": str(exc), "model": "error"}

        log.info("  %-6s HF=%s(%s%%) via %s",
                 ticker, hf["sentiment"], hf["confidence"], hf.get("model", "?"))

        # 3b-pre. Auto-veto: hallucinated news
        has_headlines          = bool(headlines_map.get(ticker, []))
        gemini_reasoning_lower = rec.get("reasoning", "").lower()
        # The prompt instructs the analyst to state "No news data available"
        # when no headlines exist; if it did, it is correctly declaring absence
        # (not hallucinating), so generic price-movement verbs must not auto-veto.
        declared_no_news = "no news data available" in gemini_reasoning_lower
        analyst_referenced_news = any(
            kw in gemini_reasoning_lower for kw in NEWS_REFERENCE_KEYWORDS
        )
        if not has_headlines and analyst_referenced_news and not declared_no_news:
            auto_veto_reason = (
                "AUTO-VETO: analyst referenced news not present in data — "
                "no headlines were fetched for this ticker this cycle"
            )
            log.warning("  %-6s %s", ticker, auto_veto_reason)
            vetoed_count += 1
            decisions[ticker] = {
                "haiku_verdict":       "VETO",
                "haiku_justification": auto_veto_reason,
                "hf_sentiment":        hf.get("sentiment", "NEUTRAL"),
                "hf_confidence":       hf.get("confidence"),
                "hf_reasoning":        hf.get("reasoning", ""),
                "hf_model":            hf.get("model", ""),
                "final_action":        "VETOED",
                "auto_veto":           True,
                "gemini_batch":        ticker_batch.get(ticker),
            }
            jsonl_append(LOG_DIR / "vetoed.jsonl", {
                "timestamp":           run_ts,
                "ticker":              ticker,
                "recommendation":      action,
                "confidence":          gemini_confidence,
                "reasoning":           rec.get("reasoning", ""),
                "haiku_verdict":       "VETO",
                "haiku_justification": auto_veto_reason,
                "hf_sentiment":        hf.get("sentiment", "NEUTRAL"),
                "hf_confidence":       hf.get("confidence"),
                "hf_model":            hf.get("model", ""),
                "veto_reasons":        [auto_veto_reason],
                "source":              "auto-veto",
                "gemini_batch":        ticker_batch.get(ticker),
            })
            continue

        # 3a-pre. Pre-Haiku gate — cheap, deterministic, no Claude call.
        # Skip the metered Haiku review entirely when this candidate cannot
        # possibly be traded this run: slot / position state is knowable
        # up front and doesn't depend on Haiku's verdict. Same skip labels
        # execute_trades would assign later, so audit continuity holds.
        pre_skip = None
        if pre_check_enabled:
            if action == "BUY":
                if running_open_count >= MAX_POSITIONS:
                    pre_skip = "SKIPPED_MAX_POS"
                elif ticker in run_start_positions or ticker in run_start_pending:
                    pre_skip = "SKIPPED_OPEN"
            elif action == "SELL":
                if ticker not in run_start_positions:
                    pre_skip = "SKIPPED_NO_POS"

        if pre_skip:
            log.info("  %-6s pre-skip %s — Haiku call skipped", ticker, pre_skip)
            decisions[ticker] = {
                "haiku_verdict":       "NOT_CALLED",
                "haiku_justification": "",
                "hf_sentiment":        hf.get("sentiment", "NEUTRAL"),
                "hf_confidence":       hf.get("confidence"),
                "hf_reasoning":        hf.get("reasoning", ""),
                "hf_model":            hf.get("model", ""),
                "final_action":        pre_skip,
                "gemini_batch":        ticker_batch.get(ticker),
            }
            continue

        # 3b. Haiku risk review
        haiku_input   = build_data_block(data_map[ticker], headlines_map.get(ticker, []))
        haiku_preview = (
            f"MARKET DATA:\n{haiku_input}\n\n"
            f"ANALYST RECOMMENDATION:\n{json.dumps(rec)}"
        )
        log.info("DEBUG Haiku prompt for %s: %s", ticker, haiku_preview[:200])
        verdict, justification = run_risk_manager(haiku_input, rec, hf)
        api_calls["haiku"] += 1
        log.info("  %-6s Gemini=%s(%s%%) Haiku=%s",
                 ticker, action, gemini_confidence, verdict)

        # 3c. Three-way gate
        hf_sentiment = hf.get("sentiment", "NEUTRAL")
        # Only veto when sentiment actively CONTRADICTS the signal; NEUTRAL
        # (quiet/empty news) no longer blocks an otherwise-approved trade.
        sentiment_ok = not (
            (action == "BUY"  and hf_sentiment == "BEARISH") or
            (action == "SELL" and hf_sentiment == "BULLISH")
        )

        veto_reasons = []
        if verdict != "APPROVED":
            veto_reasons.append(f"Haiku veto: {justification}")
        if gemini_confidence < 70:
            veto_reasons.append(f"Gemini confidence too low ({gemini_confidence}% < 70%)")
        if not sentiment_ok:
            veto_reasons.append(f"HF sentiment mismatch: {action} but {hf_sentiment}")

        dec_base = {
            "haiku_verdict":       verdict,
            "haiku_justification": justification,
            "hf_sentiment":        hf_sentiment,
            "hf_confidence":       hf.get("confidence"),
            "hf_reasoning":        hf.get("reasoning", ""),
            "hf_model":            hf.get("model", ""),
            "gemini_batch":        ticker_batch.get(ticker),
        }

        if veto_reasons:
            vetoed_count += 1
            combined = " | ".join(veto_reasons)
            log.info("  %-6s VETOED: %s", ticker, combined[:100])
            decisions[ticker] = {**dec_base, "final_action": "VETOED"}
            jsonl_append(LOG_DIR / "vetoed.jsonl", {
                "timestamp":           run_ts,
                "ticker":              ticker,
                "recommendation":      action,
                "confidence":          gemini_confidence,
                "reasoning":           rec.get("reasoning", ""),
                "haiku_verdict":       verdict,
                "haiku_justification": justification,
                "hf_sentiment":        hf_sentiment,
                "hf_confidence":       hf.get("confidence"),
                "hf_model":            hf.get("model", ""),
                "veto_reasons":        veto_reasons,
                "gemini_batch":        ticker_batch.get(ticker),
            })
        else:
            decisions[ticker] = {**dec_base, "final_action": f"{action}_PENDING"}
            approved_trades.append(rec)
            # Mirror execute_trades' own open_count += 1 / -= 1 (:1105/:1145)
            # so a later candidate in this same run sees accurate remaining
            # capacity — an approved SELL earlier in the run frees a slot a
            # later BUY can fill, exactly as execute_trades allows today.
            if action == "BUY":
                running_open_count += 1
            elif action == "SELL":
                running_open_count = max(0, running_open_count - 1)

    # ── Step 4: Execution ─────────────────────────────────────────────────
    log.info("Step 4: Executing %d approved trade(s)…", len(approved_trades))
    if approved_trades:
        try:
            exec_status = execute_trades(approved_trades, data_map, alpaca)
            for rec in approved_trades:
                t = rec.get("ticker", "")
                if t not in decisions:
                    continue
                st = exec_status.get(t, "ORDER_FAILED")
                if st == "EXECUTED":
                    decisions[t]["final_action"] = f"{rec.get('recommendation')}_EXECUTED"
                else:
                    # SKIPPED_* / ORDER_FAILED — do not claim an execution that
                    # never happened (find_original_buy_reasoning keys on
                    # BUY_EXECUTED, so a false claim would mis-feed the exit analyst).
                    decisions[t]["final_action"] = st
        except Exception as exc:
            log.error("Trade execution error: %s", exc)
    else:
        log.info("  No approved trades to execute this cycle.")

    # ── Write decisions log ───────────────────────────────────────────────
    # 1. Buy/sell analysis decisions
    _empty_dec = {
        "haiku_verdict": "N/A", "haiku_justification": "",
        "hf_sentiment": "N/A", "hf_confidence": None,
        "hf_reasoning": "", "hf_model": "",
        "final_action": "NO_DATA",
        "gemini_batch": None,
    }
    for ticker, mdata in data_map.items():
        rec = recs_by_ticker.get(ticker, {})
        dec = decisions.get(ticker, _empty_dec)
        jsonl_append(LOG_DIR / "decisions.jsonl", {
            "run_timestamp":         run_ts,
            "decision_type":         "buy_analysis",
            "ticker":                ticker,
            "gemini_batch":          dec.get("gemini_batch", ticker_batch.get(ticker)),
            "current_price":         mdata.get("current_price"),
            "daily_change_pct":      mdata.get("daily_change_pct"),
            "ma5":                   mdata.get("ma5"),
            "ma20":                  mdata.get("ma20"),
            "ma50":                  mdata.get("ma50"),
            "volume":                mdata.get("volume"),
            "gemini_model":          gemini_model_used,
            "gemini_recommendation": rec.get("recommendation", "UNKNOWN"),
            "gemini_confidence":     rec.get("confidence"),
            "gemini_reasoning":      rec.get("reasoning", ""),
            "hf_sentiment":          dec.get("hf_sentiment", "N/A"),
            "hf_confidence":         dec.get("hf_confidence"),
            "hf_reasoning":          dec.get("hf_reasoning", ""),
            "hf_model":              dec.get("hf_model", ""),
            "haiku_verdict":         dec.get("haiku_verdict", "N/A"),
            "haiku_justification":   dec.get("haiku_justification", ""),
            "final_action":          dec.get("final_action", "NO_DATA"),
        })

    # 2. Exit review decisions
    for ed in exit_decisions:
        jsonl_append(LOG_DIR / "decisions.jsonl", {
            "run_timestamp": run_ts,
            **ed,
        })

    # ── Portfolio Summary + Snapshot ──────────────────────────────────────
    try:
        print_portfolio_summary(alpaca)
    except Exception as exc:
        log.error("Portfolio summary failed: %s", exc)

    snapshot_portfolio(alpaca, run_ts)

    log.info(
        "API calls this run — buy Gemini: %d, HF: %d, Haiku: %d | "
        "exit Gemini: %d, exit Haiku: %d",
        api_calls["gemini"], api_calls["hf"], api_calls["haiku"],
        api_calls.get("exit_gemini", 0), api_calls.get("exit_haiku", 0),
    )

    ai_exits_total   = len([e for e in exit_decisions if e.get("final_exit_action") in ("EXITED", "TRIMMED")])
    ai_holds_total   = len([e for e in exit_decisions if e.get("final_exit_action") == "HELD"])
    ai_vetoed_exits  = len([e for e in exit_decisions if e.get("final_exit_action") == "VETOED"])

    # Cumulative simulated fees across all executed trades ever logged
    exec_file = LOG_DIR / "executed.jsonl"
    cumulative_sim_fees = 0.0
    if exec_file.exists():
        for line in exec_file.read_text().splitlines():
            try:
                cumulative_sim_fees += json.loads(line).get("sim_total_fee", 0.0)
            except Exception:
                pass
    cumulative_sim_fees = round(cumulative_sim_fees, 4)

    jsonl_append(LOG_DIR / "run_log.jsonl", {
        "timestamp":           run_ts,
        "event":               "run_complete",
        "approved":            len(approved_trades),
        "vetoed":              vetoed_count,
        "hard_stopped":        len(hard_stopped),
        "ai_exits":            ai_exits_total,
        "ai_holds":            ai_holds_total,
        "ai_vetoed_exits":     ai_vetoed_exits,
        "tickers_ingested":    list(data_map.keys()),
        "tickers_skipped":     [t for t in TICKERS if t not in data_map],
        "api_calls":           api_calls,
        "cumulative_sim_fees": cumulative_sim_fees,
    })
    log.info("=== Run complete ===")


if __name__ == "__main__":
    main()
