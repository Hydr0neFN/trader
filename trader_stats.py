#!/usr/bin/env python3
"""LLM usage, cost and verdict stats for trader.py — read-only.

    python3 trader_stats.py                        # since the Haiku 5.5 cutover
    python3 trader_stats.py --since 2026-10-01     # naive input = US/Eastern
    python3 trader_stats.py --since 2026-10-08T22:17:00+08:00 --json

Reads trade_logs/llm_calls.jsonl and trade_logs/decisions.jsonl and prints, for
the window:
  * one row per model: calls, errors, cost_usd, input / output / cache tokens,
    cache-hit rate, median output_tokens
  * the haiku_verdict and final_action distribution of buy decisions, and the
    haiku_exit_verdict / final_exit_action distribution of exit decisions

Never writes, never takes trader.lock, opens both files read-only — safe to run
mid-session, over ssh, or from an agent that must not mutate anything.

TIME ZONES. Every timestamp the bot writes is ET-aware (`now_et()`, offset
-04:00 / -05:00), so `--since` is compared as an aware instant. A `--since`
without an offset is interpreted as **US/Eastern**, not the host's Asia/Shanghai
or your own zone. The default is the Haiku 4.5 -> 5.5 cutover, pinned in the
vault as 2026-10-08 22:17 CST (UTC+8) = 10:17 ET. decisions.jsonl rows carry no
model field, so they are split by timestamp; llm_calls rows carry `model`.
Any verdict-rate comparison that spans that instant is invalid.
"""
import argparse
import collections
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
LOG_DIR = Path(os.environ.get("TRADE_LOGS", str(Path.home() / "trade_logs")))
CHUNK = 1 << 20  # backward-scan read size; tests shrink it to cross boundaries
CUTOVER = datetime(2026, 10, 8, 22, 17, tzinfo=timezone(timedelta(hours=8)))  # CST


def parse_ts(raw, default_tz=ET):
    """ISO-8601 -> aware datetime. Naive values are taken as `default_tz`."""
    dt = datetime.fromisoformat(str(raw))
    return dt if dt.tzinfo else dt.replace(tzinfo=default_tz)


def iter_since(path, since, key):
    """Yield dict rows with row[key] >= since, newest-last, from a read-only
    chronological jsonl. Scans backwards in chunks so a 90 MB decisions.jsonl
    costs only the window; stops at the first row older than `since`."""
    path = Path(path)
    if not path.exists():
        return
    out = []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos, carry = f.tell(), b""
        done = False
        while pos > 0 and not done:
            step = min(CHUNK, pos)
            pos -= step
            f.seek(pos)
            parts = (f.read(step) + carry).split(b"\n")
            # parts[0] may be a partial line unless we reached the file start
            carry, lines = (parts[0], parts[1:]) if pos > 0 else (b"", parts)
            for line in reversed(lines):
                row = _loads(line)
                if row is None:
                    continue
                try:
                    ts = parse_ts(row[key])
                except (KeyError, ValueError):
                    continue
                if ts < since:
                    done = True
                    break
                out.append(row)
    yield from reversed(out)


def _loads(line):
    line = line.strip()
    if not line:
        return None
    try:
        row = json.loads(line)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def model_table(rows):
    agg = collections.OrderedDict()
    for r in rows:
        a = agg.setdefault(r.get("model") or "?", {
            "path": set(), "calls": 0, "errors": 0, "cost_usd": 0.0,
            "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "_out": []})
        a["path"].add(r.get("path") or "?")
        a["calls"] += 1
        a["errors"] += 1 if r.get("error") else 0
        a["cost_usd"] += r.get("cost_usd") or 0.0
        a["input"] += r.get("input_tokens") or 0
        a["output"] += r.get("output_tokens") or 0
        a["cache_write"] += r.get("cache_creation_input_tokens") or 0
        a["cache_read"] += r.get("cache_read_input_tokens") or 0
        if r.get("output_tokens") is not None:
            a["_out"].append(r["output_tokens"])
    table = []
    for model, a in agg.items():
        prompt = a["input"] + a["cache_write"] + a["cache_read"]
        table.append({
            "model": model, "path": "/".join(sorted(a["path"])),
            "calls": a["calls"], "errors": a["errors"],
            "cost_usd": round(a["cost_usd"], 6),
            "input_tokens": a["input"], "output_tokens": a["output"],
            "cache_write_tokens": a["cache_write"], "cache_read_tokens": a["cache_read"],
            "cache_hit_rate": round(a["cache_read"] / prompt, 4) if prompt else None,
            "median_output_tokens": statistics.median(a["_out"]) if a["_out"] else None,
        })
    return table


def verdict_dist(rows):
    d = {"buy": {"haiku_verdict": collections.Counter(), "final_action": collections.Counter()},
         "exit": {"haiku_exit_verdict": collections.Counter(),
                  "final_exit_action": collections.Counter()}}
    for r in rows:
        kind = "exit" if "final_exit_action" in r or "haiku_exit_verdict" in r else "buy"
        for field, counter in d[kind].items():
            if field in r:
                counter[str(r[field])] += 1
    return {k: {f: dict(c.most_common()) for f, c in v.items()} for k, v in d.items()}


def build(since, log_dir=LOG_DIR):
    calls = list(iter_since(Path(log_dir) / "llm_calls.jsonl", since, "timestamp"))
    decs = list(iter_since(Path(log_dir) / "decisions.jsonl", since, "run_timestamp"))
    return {
        "since": since.astimezone(ET).isoformat(),
        "since_note": "ET; decisions rows have no model field, split by timestamp",
        "llm_calls_rows": len(calls), "decisions_rows": len(decs),
        "models": model_table(calls), "verdicts": verdict_dist(decs),
    }


def render(rep):
    cols = [("model", "model", "<"), ("path", "path", "<"), ("calls", "calls", ">"),
            ("errors", "err", ">"), ("cost_usd", "cost_usd", ">"),
            ("input_tokens", "in_tok", ">"), ("output_tokens", "out_tok", ">"),
            ("cache_write_tokens", "c_write", ">"), ("cache_read_tokens", "c_read", ">"),
            ("cache_hit_rate", "hit%", ">"), ("median_output_tokens", "med_out", ">")]

    def cell(row, k):
        v = row[k]
        if v is None:
            return "-"
        if k == "cache_hit_rate":
            return f"{v * 100:.1f}"
        if k == "cost_usd":
            return f"{v:.4f}"
        return str(v)

    lines = [f"window: since {rep['since']}  (llm_calls rows {rep['llm_calls_rows']}, "
             f"decisions rows {rep['decisions_rows']})", ""]
    rows = [[cell(r, k) for k, _, _ in cols] for r in rep["models"]]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
              for i, (_, h, _) in enumerate(cols)]
    lines.append("  ".join(f"{h:{a}{w}}" for (_, h, a), w in zip(cols, widths)))
    for r in rows:
        lines.append("  ".join(f"{c:{a}{w}}" for c, (_, _, a), w in zip(r, cols, widths)))
    if not rows:
        lines.append("(no llm_calls rows in window)")
    for kind, fields in rep["verdicts"].items():
        for field, dist in fields.items():
            lines += ["", f"{kind}: {field}"]
            lines += [f"  {k:<28}{v:>6}" for k, v in dist.items()] or ["  (none)"]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", help="ISO ts; naive = US/Eastern. Default: Haiku 5.5 cutover "
                    "(2026-10-08T22:17+08:00)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--log-dir", default=str(LOG_DIR), help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    since = parse_ts(a.since) if a.since else CUTOVER
    rep = build(since, a.log_dir)
    print(json.dumps(rep, indent=2) if a.json else render(rep))


if __name__ == "__main__":
    sys.exit(main())
