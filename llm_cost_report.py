#!/usr/bin/env python3
"""Summarise trade_logs/llm_calls.jsonl — what trader.py actually spends.

Written 2026-08-15, when the per-call logging was added. Read-only: it never
touches the bot, so it is safe to run mid-session, during market hours, from
cron, or from an ssh one-liner.

    python3 llm_cost_report.py            # last 14 days + totals
    python3 llm_cost_report.py --days 1   # today only
    python3 llm_cost_report.py --days 0   # whole file

Reported separately and never summed together:
  metered      -> real Anthropic API dollars (Haiku buy gate)
  subscription -> the Pro-plan SDK path (Sonnet exits), always cost_usd 0.0
The cache columns are the point of the whole exercise: `cache_read` tokens bill
at a tenth of fresh input, so a falling hit-rate is a rising bill even when the
call count is flat.
"""
import argparse
import collections
import json
import os
import sys

from pathlib import Path

# Matches trader.py, which writes to Path.home() / "trade_logs".
LOG = os.environ.get("LLM_CALLS_LOG",
                     str(Path.home() / "trade_logs" / "llm_calls.jsonl"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14,
                    help="how many recent calendar days to break out (0 = all)")
    ap.add_argument("--file", default=LOG)
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print(f"{args.file} does not exist yet.")
        print("Expected: the first record is written by the next real cron run")
        print("(trader.py only reaches a Claude call when the market is open).")
        return 1

    rows = []
    bad = 0
    with open(args.file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1

    if not rows:
        print(f"{args.file} exists but holds no parseable records "
              f"({bad} unparseable lines).")
        return 1

    days = sorted({r.get("timestamp", "")[:10] for r in rows})
    keep = set(days if args.days == 0 else days[-args.days:])

    def agg(subset):
        a = dict(n=0, cost=0.0, fresh=0, cached=0, cache_write=0, out=0,
                 errors=0, latency=0.0, latency_n=0, unpriced=0)
        for r in subset:
            a["n"] += 1
            c = r.get("cost_usd")
            if c is None:
                # _claude_cost_usd returns None for a model missing from
                # CLAUDE_PRICES -- unpriced, deliberately not counted as $0.
                a["unpriced"] += 1
            else:
                a["cost"] += c
            a["fresh"] += r.get("input_tokens") or 0
            a["cached"] += r.get("cache_read_input_tokens") or 0
            a["cache_write"] += r.get("cache_creation_input_tokens") or 0
            a["out"] += r.get("output_tokens") or 0
            if r.get("error"):
                a["errors"] += 1
            lat = r.get("latency_ms")
            if lat is not None:
                a["latency"] += lat
                a["latency_n"] += 1
        return a

    def hit_rate(a):
        seen = a["fresh"] + a["cached"]
        return (100.0 * a["cached"] / seen) if seen else 0.0

    def line(label, a):
        lat = (a["latency"] / a["latency_n"]) if a["latency_n"] else 0.0
        return ("%-12s %6d calls  $%8.4f  cache-hit %5.1f%%  "
                "in %8d fresh / %9d cached / %7d cw  out %7d  "
                "err %3d  %6.0f ms avg" % (
                    label, a["n"], a["cost"], hit_rate(a), a["fresh"],
                    a["cached"], a["cache_write"], a["out"], a["errors"], lat))

    metered = [r for r in rows if r.get("billing") != "subscription"]
    subs = [r for r in rows if r.get("billing") == "subscription"]

    print("=" * 118)
    print("trader.py LLM spend  |  file %s  |  %d records, %s -> %s"
          % (args.file, len(rows), days[0], days[-1]))
    if bad:
        print("WARNING: %d unparseable line(s) skipped" % bad)
    print("=" * 118)

    print("\nLIFETIME (whole file)")
    print(line("metered", agg(metered)))
    print(line("subscript.", agg(subs)) + "   <- Pro plan, $0 metered by design")

    print("\nPER DAY (metered only%s)" %
          ("" if args.days == 0 else ", last %d days present in file" % args.days))
    per_day = collections.defaultdict(list)
    for r in metered:
        d = r.get("timestamp", "")[:10]
        if d in keep:
            per_day[d].append(r)
    for d in sorted(per_day):
        print(line(d, agg(per_day[d])))

    print("\nBY MODEL / PATH / CALL TYPE (metered)")
    for key in ("model", "path", "call_type"):
        buckets = collections.defaultdict(list)
        for r in metered:
            buckets[str(r.get(key))].append(r)
        for k in sorted(buckets):
            print(line("%s=%s" % (key[:4], k), agg(buckets[k]))[:118])

    m = agg(metered)
    if per_day:
        active = len(per_day)
        print("\nRUN RATE over the %d day(s) shown: $%.4f/day metered -> "
              "$%.2f/month at 21 trading days" % (
                  active, m["cost"] / active, (m["cost"] / active) * 21))
    if m["unpriced"]:
        print("NOTE: %d call(s) had cost_usd=null -> model missing from "
              "CLAUDE_PRICES in trader.py. Add its rates; do NOT read null as $0."
              % m["unpriced"])
    print("\nBaseline to compare against: pre-2026-08-15 spend was never logged, "
          "only estimated at $32-75 lifetime / $8-20 per month.")
    print("The 2026-08-15 change was expected to cut metered call count by "
          ">=38%. Judge it on the 'calls' column, not on dollars alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
