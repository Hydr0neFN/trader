import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask, make_response, redirect, render_template, request

import i18n
from alpaca.trading.client import TradingClient
import yfinance as yf

load_dotenv(Path.home() / ".env")

# Cap hung network calls (Alpaca, yfinance sparklines) so a stuck socket can't
# wedge a gunicorn worker past its timeout.
import socket as _socket
_socket.setdefaulttimeout(20)

# Read credentials defensively: a missing ~/.env should surface a clear message
# (in the route error banner) rather than a bare KeyError worker-boot crash.
ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
# LOG_DIR override lets a hardened (non-cron-user) service still find the logs.
LOG_DIR = Path(os.environ.get("TRADE_LOG_DIR", Path.home() / "trade_logs"))
STARTING_EQUITY = float(os.environ.get("STARTING_EQUITY", "100000"))
ET = ZoneInfo("America/New_York")

app = Flask(__name__)

# One reused client + a tiny TTL cache: account/positions only change when the
# cron runs (every 30 min), so per-request Alpaca calls (no default timeout)
# needlessly burn the shared rate-limit budget and can wedge a worker on a hang.
_client: TradingClient | None = None
_cache: dict = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_alpaca() -> TradingClient:
    global _client
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set — check ~/.env")
    if _client is None:
        _client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
    return _client


def cached(key: str, ttl: float, fn):
    """Return fn() memoized for `ttl` seconds (per-process)."""
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


_jsonl_cache: dict = {}


def read_jsonl(filename: str) -> list:
    path = LOG_DIR / filename
    if not path.exists():
        return []
    # decisions.jsonl is tens of MB and only changes when the cron runs; parsing
    # it on every request (×N routes) is what made /history exceed the worker
    # timeout. Cache by mtime so an unchanged file is parsed at most once.
    mtime = path.stat().st_mtime
    hit = _jsonl_cache.get(filename)
    if hit and hit[0] == mtime:
        return hit[1]
    lines = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    _jsonl_cache[filename] = (mtime, lines)
    return lines


def tail_jsonl(filename: str, n: int) -> list:
    """Last n parseable records of a JSONL file, read from the end.

    read_jsonl() parses the whole file, which is fine for the small logs but not
    for decisions.jsonl (~90 MB). Seeking a fixed window from the end keeps this
    O(window) no matter how large the log grows.
    """
    path = LOG_DIR / filename
    if not path.exists():
        return []
    window = 256 * 1024
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - window))
            chunk = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    # The first line is probably a fragment of a record that starts before the
    # window, so drop it whenever we did not start at byte 0.
    lines = chunk.splitlines()
    if size > window and lines:
        lines = lines[1:]
    out = []
    for line in lines[-n:]:
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _clean_model_id(raw) -> str:
    """Tidy a model id with rules only — never a name lookup, because a lookup
    table is exactly what went stale in the footer before.

    agy:Gemini 3.8 Flash (High)   → Gemini 3.8 Flash
    deepseek-ai/DeepSeek-V3.2-Exp → DeepSeek-V3.2-Exp
    claude-haiku-4-5-20251001     → claude-haiku-4-5
    """
    s = str(raw or "").strip()
    s = re.sub(r"^[A-Za-z0-9_.-]+:", "", s)   # provider scheme, e.g. "agy:"
    s = s.split("/")[-1]                       # namespace, e.g. "deepseek-ai/"
    s = re.sub(r"\s*\([^)]*\)", "", s)          # mode suffix, e.g. " (High)"
    s = re.sub(r"[-_@]\d{8}$", "", s)           # snapshot date, e.g. "-20251001"
    s = re.sub(r"[:-]latest$", "", s, flags=re.I)
    return s.strip()


def active_models() -> list:
    """The models this bot is actually running right now, newest evidence first.

    The footer used to carry a hand-written list ("Gemini 3 Flash + Claude
    Haiku") that drifted from the code: the Gemini model is chosen at runtime
    from a fallback chain or from agy, Claude answers on two different tiers,
    and the sentiment agent was missing from the list entirely. Deriving it from
    the logs means it cannot go stale again.
    """
    def _fresh(fn):
        try:
            return fn()
        except Exception:
            return []

    def _from_llm_calls():
        seen = []
        for r in tail_jsonl("llm_calls.jsonl", 200):
            m = r.get("model")
            if m and m not in seen:
                seen.append(m)
        return seen

    def _from_decisions():
        seen = []
        for r in tail_jsonl("decisions.jsonl", 120):
            for key in ("gemini_model", "hf_model"):
                m = r.get(key)
                if m and m not in ("none", "N/A") and m not in seen:
                    seen.append(m)
        return seen

    out = []
    for m in _fresh(_from_decisions) + _fresh(_from_llm_calls):
        m = _clean_model_id(m)
        if m and m not in out:
            out.append(m)
    return out[:4]


def parse_dt(s):
    """Parse an ISO timestamp, coercing naive values to UTC. None on failure."""
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def fmt_ts(ts_str: str) -> str:
    """ISO timestamp → human-readable ET string."""
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
    except Exception:
        return ts_str


def fmt_currency(val) -> str:
    try:
        return f"${float(val):,.2f}"
    except Exception:
        return "—"


def fmt_pct(val) -> str:
    try:
        return f"{float(val):+.2f}%"
    except Exception:
        return "—"


# ── Language ──────────────────────────────────────────────────────────────────
# Default follows Accept-Language, which follows the OS/browser setting; the
# navbar toggle writes a cookie that overrides it. Injected into every template
# rather than passed per-route, so a new page is bilingual by construction.
def _asset_v(name: str) -> int:
    """mtime of a file in /static, appended to its URL as ?v=.

    /static is served with `max-age=14400`, so without this a deploy leaves an
    already-open browser on the previous stylesheet for four hours — which is
    exactly what happened on 2026-09-09 and took a manual hard reload to clear.
    """
    try:
        return int((Path(app.static_folder) / name).stat().st_mtime)
    except OSError:
        return 0


@app.context_processor
def inject_i18n():
    lang = i18n.choose(
        cookie_val=request.cookies.get(i18n.COOKIE),
        query_val=request.args.get("lang"),
        accept_language=request.headers.get("Accept-Language"),
    )
    ctx = i18n.make_helpers(lang)
    ctx["js_i18n"] = json.dumps(i18n.js_table(lang))
    # 60 s is plenty: the models only change when the cron runs.
    ctx["active_models"] = cached("active_models", 60, active_models)
    ctx["theme_v"] = cached("theme_v", 10, lambda: _asset_v("theme.css"))
    return ctx


@app.route("/lang/<code>")
def set_lang(code):
    """Persist a language choice and return where the user came from."""
    lang = i18n.normalize(code) or i18n.DEFAULT_LANG
    nxt = request.args.get("next", "/")
    # Only ever redirect within this site.
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    resp = make_response(redirect(nxt, code=302))
    resp.set_cookie(i18n.COOKIE, lang, max_age=60 * 60 * 24 * 365,
                    samesite="Lax", path="/")
    return resp


# Register template filters
app.jinja_env.filters["fmt_ts"]       = fmt_ts
app.jinja_env.filters["fmt_currency"] = fmt_currency
app.jinja_env.filters["fmt_pct"]      = fmt_pct


def _sparklines(symbols: list) -> dict:
    """Last ~7 daily closes per symbol for inline sparklines. Best-effort: a
    yfinance hiccup returns {} so the table still renders. Cached by the caller
    (30 min) so this network hit is rare; socket default-timeout caps any hang."""
    if not symbols:
        return {}
    try:
        data  = yf.download(symbols, period="7d", interval="1d",
                            progress=False, threads=False)
        close = data["Close"]
        out: dict = {}
        for s in symbols:
            try:
                series = close[s] if len(symbols) > 1 else close
                out[s] = [round(float(x), 2) for x in series.dropna().tolist()][-7:]
            except Exception:
                out[s] = []
        return out
    except Exception:
        return {}


# ── Routes ────────────────────────────────────────────────────────────────────

def _position_rows() -> tuple[list, str | None]:
    """(rows, error) for the open-positions table. Shared by the overview page;
    both callers hit the same 20 s account/positions cache, so embedding the
    table costs no extra Alpaca traffic."""
    rows: list = []
    try:
        client    = get_alpaca()
        account   = cached("account", 20, client.get_account)
        positions = cached("positions", 20, client.get_all_positions)
        port_val  = float(account.portfolio_value) if account else 0.0

        for p in positions:
            mv = float(p.market_value)
            rows.append({
                "symbol":       p.symbol,
                "qty":          float(p.qty),
                "avg_entry":    float(p.avg_entry_price),
                "current":      float(p.current_price),
                "market_value": mv,
                "unreal_pl":    float(p.unrealized_pl),
                "unreal_plpct": float(p.unrealized_plpc) * 100,
                # PositionSide is a str-Enum: str(p.side) → "PositionSide.long".
                # Use .value so the template's r.side == 'LONG' check matches.
                "side":         str(getattr(p.side, "value", p.side)).upper(),
                # Portfolio weight — share of total equity in this position.
                "allocation":   (mv / port_val * 100) if port_val else 0.0,
            })
        syms   = [r["symbol"] for r in rows]
        sparks = cached("sparklines_" + ",".join(sorted(syms)), 1800, lambda: _sparklines(syms))
        for r in rows:
            r["spark"] = sparks.get(r["symbol"], [])
    except Exception as exc:
        return [], str(exc)

    rows.sort(key=lambda r: r["market_value"], reverse=True)
    return rows, None


@app.route("/")
def overview():
    account_data = {}
    error        = None

    try:
        client    = get_alpaca()
        account   = cached("account", 20, client.get_account)
        positions = cached("positions", 20, client.get_all_positions)

        equity      = float(account.equity)
        last_eq     = float(account.last_equity)
        port_val    = float(account.portfolio_value)
        cash        = float(account.cash)
        day_pnl     = equity - last_eq
        day_pnl_pct = (day_pnl / last_eq * 100) if last_eq else 0
        total_pnl     = port_val - STARTING_EQUITY  # configurable paper baseline
        total_pnl_pct = total_pnl / STARTING_EQUITY * 100 if STARTING_EQUITY else 0

        account_data = {
            "portfolio_value": port_val,
            "cash":            cash,
            "equity":          equity,
            "day_pnl":         day_pnl,
            "day_pnl_pct":     day_pnl_pct,
            "total_pnl":       total_pnl,
            "total_pnl_pct":   total_pnl_pct,
            "open_positions":  len(positions),
        }
    except Exception as exc:
        error = str(exc)

    # Last run info
    run_logs    = read_jsonl("run_log.jsonl")
    last_run    = next((r for r in reversed(run_logs) if r.get("event") == "run_complete"), None)
    last_run_ts = last_run.get("timestamp", "Never") if last_run else "Never"

    # Cumulative simulated fees — sum sim_total_fee from all executed trades
    cumulative_sim_fees = round(
        sum(r.get("sim_total_fee", 0.0) for r in read_jsonl("executed.jsonl")), 4
    )
    net_pnl = round(account_data.get("total_pnl", 0.0) - cumulative_sim_fees, 4) \
              if account_data else None

    # Portfolio history — pass ALL points with raw ISO ts for JS period filtering
    history    = read_jsonl("portfolio_history.jsonl")
    chart_data = [
        {
            "ts":    h["timestamp"],
            "label": fmt_ts(h["timestamp"]),
            "value": round(h.get("portfolio_value", 0), 2),
        }
        for h in history if h.get("timestamp")   # skip schema-drifted lines
    ]

    rows, positions_error = _position_rows()

    return render_template(
        "overview.html",
        account=account_data,
        error=error,
        last_run_ts=last_run_ts,
        last_run=last_run,
        chart_data=json.dumps(chart_data),
        cumulative_sim_fees=cumulative_sim_fees,
        net_pnl=net_pnl,
        rows=rows,
        positions_error=positions_error,
    )


@app.route("/positions")
def positions():
    """Holdings now live on the overview; keep the old URL working."""
    return redirect("/", code=302)


@app.route("/history")
def history():
    ticker_filter = request.args.get("ticker", "").upper().strip()
    date_from     = request.args.get("from", "")
    date_to       = request.args.get("to", "")

    executed = read_jsonl("executed.jsonl")
    vetoed   = read_jsonl("vetoed.jsonl")

    # Build decisions lookup by ticker, pre-parsing each run_timestamp ONCE.
    # decisions.jsonl has tens of thousands of records; parsing datetimes inside
    # find_decision (called per executed/vetoed row) was O(rows × decisions) and
    # blew the worker timeout. Pre-parsing here makes each match O(candidates)
    # comparisons with no repeated parsing.
    all_decisions = read_jsonl("decisions.jsonl")
    dec_by_ticker: dict = {}
    for d in all_decisions:
        dt = parse_dt(d.get("run_timestamp"))
        dec_by_ticker.setdefault(d.get("ticker", ""), []).append((dt, d))

    def find_decision(ticker: str, ts_str: str, want_type: str = "") -> dict:
        """Closest-in-time decision for (ticker, ts), within one cron interval.

        Tolerates malformed/naive/null timestamps (no 500), rejects matches more
        than 30 min away (no unrelated-run reasoning in the modal), prefers
        want_type. Candidates carry pre-parsed datetimes.
        """
        ts = parse_dt(ts_str)
        if ts is None:
            return {}
        candidates = dec_by_ticker.get(ticker, [])
        if want_type:
            typed = [(dt, d) for dt, d in candidates if d.get("decision_type") == want_type]
            candidates = typed or candidates
        best = None
        best_delta = None
        for dt, d in candidates:
            if dt is None:
                continue
            delta = abs((dt - ts).total_seconds())
            if best_delta is None or delta < best_delta:
                best_delta, best = delta, d
        if best is None or best_delta > 1800:
            return {}
        return best

    # Tag raw rows, combine, sort, and FILTER before the expensive per-row
    # decision enrichment. With thousands of historical vetoes, enriching every
    # row blew the worker timeout; we only ever render one page, so enrich only
    # the rows that survive filtering and the page cap.
    MAX_ROWS = 400
    raw = [{**e, "_type": "executed"} for e in executed] + \
          [{**v, "_type": "vetoed"} for v in vetoed]
    raw.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

    if ticker_filter:
        raw = [r for r in raw if r.get("ticker", "") == ticker_filter]
    if date_from:
        raw = [r for r in raw if r.get("timestamp", "") >= date_from]
    if date_to:
        raw = [r for r in raw if r.get("timestamp", "") <= date_to + "T23:59:59"]

    total_matched = len(raw)
    page = raw[:MAX_ROWS]

    combined = []
    for r in page:
        ticker = r.get("ticker", "")
        ts_str = r.get("timestamp", "")
        if r["_type"] == "executed":
            want = "buy_analysis" if str(r.get("action", "")).upper() == "BUY" else ""
            dec = find_decision(ticker, ts_str, want)
            row = {**dec, **r}
            row["haiku_verdict"] = row.get("haiku_verdict") or "APPROVED"
            # Exit-driven SELLs log under key "reason"; normalize to "reasoning"
            # so the table/modal renders it instead of "—".
            if not row.get("reasoning") and row.get("reason"):
                row["reasoning"] = row["reason"]
        else:
            dec = find_decision(ticker, ts_str, "buy_analysis")
            row = {**dec, **r}
            row["action"]        = row.get("recommendation", "")
            row["haiku_verdict"] = row.get("haiku_verdict") or "VETO"
        combined.append(row)

    # Reuse already-loaded executed/vetoed instead of re-reading both files.
    all_tickers = sorted({r.get("ticker", "") for r in (executed + vetoed)})

    # One payload for the page. "</" is escaped so a reasoning string containing
    # "</script>" cannot close the block it is embedded in.
    rows_json = json.dumps(combined, default=str).replace("</", "<\\/")

    return render_template(
        "history.html",
        rows=combined,
        rows_json=rows_json,
        all_tickers=all_tickers,
        ticker_filter=ticker_filter,
        date_from=date_from,
        date_to=date_to,
        total_matched=total_matched,
    )


@app.route("/decisions")
def decisions():
    all_decisions = read_jsonl("decisions.jsonl")
    run_logs      = read_jsonl("run_log.jsonl")

    run_timestamps = []
    seen = set()
    for r in reversed(run_logs):
        ts = r.get("timestamp", "")
        if ts and ts not in seen and r.get("event") == "run_complete":
            run_timestamps.append(ts)
            seen.add(ts)

    selected_ts   = request.args.get("run",    run_timestamps[0] if run_timestamps else "")
    ticker_filter = request.args.get("ticker", "").upper().strip()
    action_filter = request.args.get("action", "").upper().strip()
    dtype_filter  = request.args.get("dtype",  "").strip()

    # All decisions for this run (for dropdown population)
    all_run_decisions = [d for d in all_decisions if d.get("run_timestamp") == selected_ts]
    all_tickers       = sorted({d.get("ticker", "") for d in all_run_decisions})

    run_decisions = list(all_run_decisions)
    if ticker_filter:
        run_decisions = [d for d in run_decisions if d.get("ticker", "") == ticker_filter]
    if action_filter:
        run_decisions = [
            d for d in run_decisions
            if d.get("gemini_recommendation", "").upper() == action_filter
        ]
    if dtype_filter:
        run_decisions = [
            d for d in run_decisions
            if d.get("decision_type", "buy_analysis") == dtype_filter
        ]

    # Sort: exit reviews first (yellow cards), then buy analysis sorted by ticker
    run_decisions.sort(key=lambda d: (
        0 if d.get("decision_type") == "exit_review" else 1,
        d.get("ticker", ""),
    ))

    return render_template(
        "decisions.html",
        run_decisions=run_decisions,
        run_timestamps=run_timestamps,
        selected_ts=selected_ts,
        all_tickers=all_tickers,
        ticker_filter=ticker_filter,
        action_filter=action_filter,
        dtype_filter=dtype_filter,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
