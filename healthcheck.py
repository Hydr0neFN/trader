#!/usr/bin/env python3
"""External heartbeat watchdog for trader.py.

Why external (not an in-process self-test): the outage that motivated this ran
for days because trader.py crashed at *import time* (a missing dependency) —
before main() or any try/except could run, so no in-process guard could ever
have fired. The only durable signal is "did the process finish a run recently?"

trader.py appends one line to run_log.jsonl on every cron run — `run_complete`
when it trades, `market_closed` when the market is shut. A crash-at-import
writes nothing, so the newest line simply stops advancing. This watchdog reads
that file, and if the newest line is stale *while the US market is open* (when
cron is firing every 30 min), it raises an alert.

Holidays don't false-alarm: on a closed day trader.py still runs and logs
`market_closed`, keeping the file fresh, so no naive holiday table is needed.

Alert channels (all optional, no outward send by default):
  - always: append to trade_logs/healthcheck.log and touch trade_logs/STALE
  - HEALTHCHECK_NTFY_TOPIC  -> POST to https://ntfy.sh/<topic>
  - HEALTHCHECK_WEBHOOK     -> POST {"text": msg} as JSON

Exit code: 0 healthy, 1 stale (so cron `|| ...` can react too).

Cron (arm alongside trader.py, weekday market-hours in your server TZ):
  */30 21-23 * * 1-5 /usr/bin/python3 /root/trader/healthcheck.py
  */30 0-4  * * 2-6 /usr/bin/python3 /root/trader/healthcheck.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LOG_DIR      = Path(os.environ.get("TRADER_LOG_DIR", Path.home() / "trade_logs"))
RUN_LOG      = LOG_DIR / "run_log.jsonl"
HEALTH_LOG   = LOG_DIR / "healthcheck.log"
STALE_MARKER = LOG_DIR / "STALE"
MAX_AGE_MIN  = int(os.environ.get("HEALTHCHECK_MAX_AGE_MIN", "90"))
ET           = ZoneInfo("America/New_York")


def market_window_now() -> bool:
    """True during the US regular-session window (weekday 09:30-16:00 ET).

    Naive by design — no holiday table. On holidays trader.py still logs
    `market_closed`, so the freshness check below won't trip anyway.
    """
    n = datetime.now(ET)
    if n.weekday() >= 5:  # Sat/Sun
        return False
    return dtime(9, 30) <= n.time() < dtime(16, 0)


def last_run_log_ts() -> datetime | None:
    """Timestamp of the newest run_log.jsonl line, or None if unreadable."""
    if not RUN_LOG.exists():
        return None
    last = None
    # Walk from the end cheaply enough for this file size; fall back to full read.
    try:
        for line in RUN_LOG.read_text().splitlines():
            if line.strip():
                last = line
    except OSError:
        return None
    if not last:
        return None
    try:
        ts = json.loads(last).get("timestamp")
        return datetime.fromisoformat(ts) if ts else None
    except (ValueError, json.JSONDecodeError):
        return None


def notify(msg: str) -> None:
    """Fan out the alert to every configured channel; never raise."""
    ts = datetime.now(timezone.utc).isoformat()
    try:
        with HEALTH_LOG.open("a") as f:
            f.write(f"{ts} STALE {msg}\n")
        STALE_MARKER.write_text(f"{ts} {msg}\n")
    except OSError:
        pass

    topic = os.environ.get("HEALTHCHECK_NTFY_TOPIC")
    if topic:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{topic}",
                data=msg.encode(),
                headers={"Title": "trader watchdog", "Priority": "high"},
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception:
            pass

    hook = os.environ.get("HEALTHCHECK_WEBHOOK")
    if hook:
        try:
            req = urllib.request.Request(
                hook,
                data=json.dumps({"text": msg}).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception:
            pass


def main() -> int:
    if not market_window_now():
        return 0  # quiet outside regular session; overnight/weekend gaps are normal

    ts = last_run_log_ts()
    now = datetime.now(timezone.utc)
    if ts is None:
        notify("run_log.jsonl missing or unreadable during market hours")
        return 1

    age_min = (now - ts.astimezone(timezone.utc)).total_seconds() / 60
    if age_min > MAX_AGE_MIN:
        notify(f"trader stale: last run_log line {age_min:.0f} min ago "
               f"(>{MAX_AGE_MIN}) — cron/import likely broken")
        return 1

    # Healthy — clear any prior stale marker.
    try:
        STALE_MARKER.unlink()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
