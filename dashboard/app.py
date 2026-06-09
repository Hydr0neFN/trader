import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask, render_template, request
from alpaca.trading.client import TradingClient

load_dotenv(Path.home() / ".env")

ALPACA_API_KEY    = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
LOG_DIR = Path.home() / "trade_logs"
ET = ZoneInfo("America/New_York")

app = Flask(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_alpaca() -> TradingClient:
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)


def read_jsonl(filename: str) -> list:
    path = LOG_DIR / filename
    if not path.exists():
        return []
    lines = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return lines


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


# Register template filters
app.jinja_env.filters["fmt_ts"]       = fmt_ts
app.jinja_env.filters["fmt_currency"] = fmt_currency
app.jinja_env.filters["fmt_pct"]      = fmt_pct


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def overview():
    account_data = {}
    error        = None

    try:
        client    = get_alpaca()
        account   = client.get_account()
        positions = client.get_all_positions()

        equity      = float(account.equity)
        last_eq     = float(account.last_equity)
        port_val    = float(account.portfolio_value)
        cash        = float(account.cash)
        day_pnl     = equity - last_eq
        day_pnl_pct = (day_pnl / last_eq * 100) if last_eq else 0
        total_pnl     = port_val - 100_000  # paper account starts at $100k
        total_pnl_pct = total_pnl / 100_000 * 100

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
        for h in history
    ]

    return render_template(
        "overview.html",
        account=account_data,
        error=error,
        last_run_ts=last_run_ts,
        last_run=last_run,
        chart_data=json.dumps(chart_data),
        cumulative_sim_fees=cumulative_sim_fees,
        net_pnl=net_pnl,
    )


@app.route("/positions")
def positions():
    rows  = []
    error = None

    try:
        client    = get_alpaca()
        positions = client.get_all_positions()

        for p in positions:
            rows.append({
                "symbol":       p.symbol,
                "qty":          float(p.qty),
                "avg_entry":    float(p.avg_entry_price),
                "current":      float(p.current_price),
                "market_value": float(p.market_value),
                "unreal_pl":    float(p.unrealized_pl),
                "unreal_plpct": float(p.unrealized_plpc) * 100,
                "side":         str(p.side).upper(),
            })
    except Exception as exc:
        error = str(exc)

    rows.sort(key=lambda r: r["market_value"], reverse=True)
    return render_template("positions.html", rows=rows, error=error)


@app.route("/history")
def history():
    ticker_filter = request.args.get("ticker", "").upper().strip()
    date_from     = request.args.get("from", "")
    date_to       = request.args.get("to", "")

    executed = read_jsonl("executed.jsonl")
    vetoed   = read_jsonl("vetoed.jsonl")

    # Build decisions lookup by ticker for detail enrichment
    all_decisions = read_jsonl("decisions.jsonl")
    dec_by_ticker: dict = {}
    for d in all_decisions:
        dec_by_ticker.setdefault(d.get("ticker", ""), []).append(d)

    def find_decision(ticker: str, ts_str: str) -> dict:
        """Return the decisions record closest in time to ts_str for ticker."""
        candidates = dec_by_ticker.get(ticker, [])
        if not candidates or not ts_str:
            return {}
        try:
            ts = datetime.fromisoformat(ts_str)
        except Exception:
            return {}
        return min(
            candidates,
            key=lambda d: abs(
                (datetime.fromisoformat(d.get("run_timestamp", ts_str)) - ts).total_seconds()
            ),
        )

    enriched_executed = []
    for e in executed:
        dec = find_decision(e.get("ticker", ""), e.get("timestamp", ""))
        row = {**dec, **e, "_type": "executed"}
        row["haiku_verdict"] = row.get("haiku_verdict") or "APPROVED"
        enriched_executed.append(row)

    enriched_vetoed = []
    for v in vetoed:
        dec = find_decision(v.get("ticker", ""), v.get("timestamp", ""))
        row = {**dec, **v, "_type": "vetoed"}
        row["action"]        = row.get("recommendation", "")
        row["haiku_verdict"] = row.get("haiku_verdict") or "VETO"
        enriched_vetoed.append(row)

    combined = enriched_executed + enriched_vetoed
    combined.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

    # Filters
    if ticker_filter:
        combined = [r for r in combined if r.get("ticker", "") == ticker_filter]
    if date_from:
        combined = [r for r in combined if r.get("timestamp", "") >= date_from]
    if date_to:
        combined = [r for r in combined if r.get("timestamp", "") <= date_to + "T23:59:59"]

    all_tickers = sorted(
        {r.get("ticker", "") for r in (read_jsonl("executed.jsonl") + read_jsonl("vetoed.jsonl"))}
    )

    def row_json(r: dict) -> str:
        return json.dumps({k: v for k, v in r.items() if k != "_type"}, default=str)

    return render_template(
        "history.html",
        rows=combined,
        row_json=row_json,
        all_tickers=all_tickers,
        ticker_filter=ticker_filter,
        date_from=date_from,
        date_to=date_to,
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
