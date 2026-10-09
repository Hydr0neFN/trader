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
  - SMTP email (Gmail by default) -> configured by a root-only KEY=VALUE file, not
                               env/crontab (a crontab is shown in listings, and the
                               password would leak). Path: $HEALTHCHECK_SMTP_FILE,
                               default /root/.healthcheck-smtp; no file = email off.
                               Keys: SMTP_HOST (smtp.gmail.com), SMTP_PORT (587; 465
                               uses SMTP_SSL), SMTP_USER, SMTP_PASS (an App Password;
                               spaces are stripped), SMTP_TO (comma-separated),
                               SMTP_FROM (default SMTP_USER). USER, PASS and TO are
                               required, else email stays off.
                               ntfy.sh's own anonymous `Email:` header is rejected by
                               the server (HTTP 400, code 40053) and kills the push
                               too, so it is deliberately not supported.

Check the setup without waiting for an incident:
  python3 healthcheck.py --test-email
sends one test message through every configured channel, prints which accepted it,
and exits 0 if at least one did, else 1. It writes no marker file.

Exit code: 0 healthy, 1 stale (so cron `|| ...` can react too).

Cron (arm alongside trader.py, weekday market-hours in your server TZ):
  */30 21-23 * * 1-5 /usr/bin/python3 /root/trader/healthcheck.py
  */30 0-4  * * 2-6 /usr/bin/python3 /root/trader/healthcheck.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
from datetime import datetime, time as dtime, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

LOG_DIR      = Path(os.environ.get("TRADER_LOG_DIR", Path.home() / "trade_logs"))
RUN_LOG      = LOG_DIR / "run_log.jsonl"
HEALTH_LOG   = LOG_DIR / "healthcheck.log"
STALE_MARKER = LOG_DIR / "STALE"
DEGRADED_MARKER = LOG_DIR / "DEGRADED"
# Own marker (and so its own rate limit) for the DeepSeek breaker, so a sentiment
# alert can never suppress a DeepSeek one or the other way round.
DEEPSEEK_MARKER = LOG_DIR / "DEEPSEEK_DOWN"
MAX_AGE_MIN  = int(os.environ.get("HEALTHCHECK_MAX_AGE_MIN", "90"))
# Re-alert on a still-degraded LLM chain at most this often, so a multi-day
# outage is loud on day one without spamming every 30 min after that.
DEGRADED_REALERT_H = int(os.environ.get("HEALTHCHECK_DEGRADED_REALERT_H", "12"))
# Consecutive `clock_check_failed` runs (30 min apart) before alerting. One is a
# blip trader.py already fails closed on; two means an hour of no trading.
CLOCK_FAIL_ALERT_N = int(os.environ.get("HEALTHCHECK_CLOCK_FAIL_N", "2"))
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


def trailing_clock_failures() -> int:
    """How many of the newest run_log lines in a row are `clock_check_failed`.

    Such a run still writes a line, so the freshness check above passes; this
    is the only thing that notices an Alpaca outage during market hours.
    """
    if not RUN_LOG.exists():
        return 0
    try:
        lines = [l for l in RUN_LOG.read_text().splitlines() if l.strip()]
    except OSError:
        return 0
    n = 0
    for line in reversed(lines):
        try:
            event = json.loads(line).get("event")
        except json.JSONDecodeError:
            break
        if event != "clock_check_failed":
            break
        n += 1
    return n


def last_run_complete() -> dict | None:
    """The newest `run_complete` record, or None if there is not one."""
    if not RUN_LOG.exists():
        return None
    found = None
    try:
        for line in RUN_LOG.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "run_complete":
                found = rec
    except OSError:
        return None
    return found


SMTP_FILE_DEFAULT = "/root/.healthcheck-smtp"


def _smtp_config() -> dict | None:
    """Parse the root-only SMTP KEY=VALUE file; None if email is not set up.

    Tiny stdlib parser: `#` comment lines and blanks are skipped, values may be
    wrapped in single or double quotes. Email counts as configured only when the
    file exists and has SMTP_USER, SMTP_PASS and SMTP_TO.
    """
    path = os.environ.get("HEALTHCHECK_SMTP_FILE") or SMTP_FILE_DEFAULT
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return None
    kv: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        kv[key.strip().upper()] = val
    user = kv.get("SMTP_USER", "").strip()
    # Google shows App Passwords in 4-char groups; the spaces are not part of it.
    password = "".join(kv.get("SMTP_PASS", "").split())
    to = [a.strip() for a in kv.get("SMTP_TO", "").split(",") if a.strip()]
    if not (user and password and to):
        return None
    try:
        port = int(kv.get("SMTP_PORT") or 587)
    except ValueError:
        port = 587
    return {
        "host": kv.get("SMTP_HOST") or "smtp.gmail.com",
        "port": port,
        "user": user,
        "password": password,
        "to": to,
        "from": kv.get("SMTP_FROM") or user,
    }


# Short reason of the most recent failed email, so --test-email can show it.
_last_email_error = ""


def _send_email(cfg: dict, msg: str, title: str) -> bool:
    """Send one email via SMTP; never raise, never log the password."""
    global _last_email_error
    now = datetime.now(timezone.utc).isoformat()
    try:
        m = EmailMessage()
        m["Subject"] = f"[trader] {title}"
        m["From"] = cfg["from"]
        m["To"] = ", ".join(cfg["to"])
        m.set_content(f"{msg}\n\n{now}")
        # smtplib's own default context does not verify the server certificate,
        # which would hand SMTP_PASS to anything that can intercept the hop.
        tls = ssl.create_default_context()
        if cfg["port"] == 465:
            conn = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=20, context=tls)
        else:
            conn = smtplib.SMTP(cfg["host"], cfg["port"], timeout=20)
        with conn as s:
            if cfg["port"] != 465:
                s.starttls(context=tls)
            s.login(cfg["user"], cfg["password"])
            s.send_message(m)
        return True
    except Exception as e:
        reason = " ".join(str(e).split()).replace(cfg["password"], "***")[:120]
        _last_email_error = f"{type(e).__name__}: {reason}"
        try:
            with HEALTH_LOG.open("a") as f:
                f.write(f"{now} EMAIL_FAIL {_last_email_error}\n")
        except OSError:
            pass
        return False


def _send_channels(msg: str, title: str, priority: str) -> dict[str, bool]:
    """Try every configured channel; map channel name -> accepted. Never raises."""
    results: dict[str, bool] = {}
    topic = os.environ.get("HEALTHCHECK_NTFY_TOPIC")
    hook = os.environ.get("HEALTHCHECK_WEBHOOK")
    if topic:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{topic}",
                data=msg.encode(),
                headers={"Title": title, "Priority": priority},
            )
            urllib.request.urlopen(req, timeout=15)
            results["ntfy"] = True
        except Exception:
            results["ntfy"] = False

    if hook:
        try:
            req = urllib.request.Request(
                hook,
                data=json.dumps({"text": msg}).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=15)
            results["webhook"] = True
        except Exception:
            results["webhook"] = False

    cfg = _smtp_config()
    if cfg:
        results["email"] = _send_email(cfg, msg, title)
    return results


def _send(msg: str, title: str, priority: str) -> bool:
    """Send msg to ntfy, the webhook and email, whichever are configured; never raise.

    True if at least one channel took it, or if none is configured (nothing to
    retry). Rate-limit markers are only written on True, so an outage of every
    channel at alert time retries next run instead of going silent for
    DEGRADED_REALERT_H.
    """
    results = _send_channels(msg, title, priority)
    return not results or any(results.values())


def notify_degraded(msg: str, marker: Path | None = None,
                    title: str = "trader degraded") -> None:
    """Alert that the bot is running but an LLM leg is dead.

    Separate from notify(): trader.py is alive, so this must not raise the
    STALE marker or change the exit code. Rate-limited via its own marker
    file -- the 2026-09 HuggingFace 402 outage ran 5 days with nothing but
    WARNING lines in cron.log, which is exactly what this closes.
    Each failure kind passes its own `marker` (default: DEGRADED_MARKER) so
    they rate-limit independently.
    """
    marker = marker or DEGRADED_MARKER
    now = datetime.now(timezone.utc)
    try:
        prev = datetime.fromisoformat(marker.read_text().split()[0])
        if (now - prev).total_seconds() < DEGRADED_REALERT_H * 3600:
            return
    except (OSError, ValueError, IndexError, TypeError):
        pass

    ts = now.isoformat()
    try:
        with HEALTH_LOG.open("a") as f:
            f.write(f"{ts} DEGRADED {msg}\n")
    except OSError:
        pass

    if _send(msg, title, "default"):
        try:
            marker.write_text(f"{ts} {msg}\n")
        except OSError:
            pass


def notify_recovered(msg: str, marker: Path, title: str) -> None:
    """One-shot "it's back" message.

    The marker is rewritten as "<original alert ts> RECOVERED", never deleted:
    its first field is what notify_degraded rate-limits on, so a flapping leg
    (down, up, down, ...) still alerts at most once per DEGRADED_REALERT_H
    instead of once per run. The RECOVERED tag is what makes this one-shot.
    """
    try:
        parts = marker.read_text().split()
    except OSError:
        return
    if not parts or "RECOVERED" in parts:
        return
    try:
        with HEALTH_LOG.open("a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} RECOVERED {msg}\n")
    except OSError:
        pass
    if _send(msg, title, "default"):
        try:
            marker.write_text(f"{parts[0]} RECOVERED\n")
        except OSError:
            pass


def notify(msg: str) -> None:
    """Fan out the alert to every configured channel; never raise."""
    ts = datetime.now(timezone.utc).isoformat()
    try:
        with HEALTH_LOG.open("a") as f:
            f.write(f"{ts} STALE {msg}\n")
        STALE_MARKER.write_text(f"{ts} {msg}\n")
    except OSError:
        pass

    _send(msg, "trader watchdog", "high")


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

    # Fresh, but only because trader.py keeps logging that it could not reach
    # the Alpaca clock -- it fails closed, so nothing is trading.
    clock_fails = trailing_clock_failures()
    if clock_fails >= CLOCK_FAIL_ALERT_N:
        notify(f"Alpaca clock check failed {clock_fails} runs in a row during "
               f"market hours -- trader is not trading")
        return 1

    # Fresh, but the run may still have flown blind: a total sentiment-chain
    # failure leaves every ticker at NEUTRAL/0 without stopping the run.
    rec = last_run_complete() or {}
    rec_age_min = None
    rec_ts = rec.get("timestamp")
    if rec_ts:
        try:
            rec_age_min = (now - datetime.fromisoformat(rec_ts)
                           .astimezone(timezone.utc)).total_seconds() / 60
        except (ValueError, TypeError):
            rec_age_min = None

    if rec_age_min is not None and rec_age_min <= MAX_AGE_MIN:
        fails = rec.get("sentiment_failures", 0)
        # `sentiment_calls` is what separates "asked and it worked" from "never
        # asked". trader.py skips the sentiment step entirely on a HOLD, so an
        # all-HOLD run reports zero failures while telling us nothing about the
        # provider chain. Clearing the marker on that would drop the rate limit
        # and re-alert the same ongoing outage on the next non-HOLD run.
        calls = rec.get("sentiment_calls")
        if fails:
            notify_degraded(
                f"sentiment LLM chain down: {fails} ticker(s) fell through every "
                f"provider (HF + Cloudflare) last run — trading on price data only"
            )
        elif calls:
            try:
                DEGRADED_MARKER.unlink()
            except OSError:
                pass

        # DeepSeek is the primary analyst; once the breaker trips the run falls
        # back to agy/Gemini and keeps trading, so nothing else would notice.
        # Old run_complete lines lack these keys and fall through both branches.
        if rec.get("deepseek_enabled"):
            ds_ok = rec.get("deepseek_ok") or 0
            if rec.get("deepseek_tripped"):
                err = rec.get("deepseek_last_error") or "unknown"
                msg = (f"DeepSeek down: circuit breaker tripped last run "
                       f"({rec.get('deepseek_failed', 0)} failed, {ds_ok} ok) -- "
                       f"bot fell back to agy/Gemini. Last error: {err}")
                # Match the status trader.py puts in the message ("deepseek HTTP
                # 401: ..."), not a bare "token" -- that also hits max_tokens errors.
                if re.search(r"\bHTTP 40[13]\b", err):
                    msg += (" | The DeepSeek userToken probably expired or was banned. "
                            "Fix: re-auth via ssh -L 4001:127.0.0.1:4000 root@192.168.1.10 "
                            "then http://127.0.0.1:4001/dashboard")
                notify_degraded(msg, DEEPSEEK_MARKER, "DeepSeek down")
            elif ds_ok > 0 and not rec.get("deepseek_failed") and DEEPSEEK_MARKER.exists():
                # A clean run (ok > 0, failed == 0) is what proves it works; a run
                # that never called DeepSeek (ok == failed == 0) or still saw
                # failures must leave the marker alone.
                notify_recovered("DeepSeek recovered: analyst calls succeed again",
                                 DEEPSEEK_MARKER, "DeepSeek recovered")

    # Healthy — clear any prior stale marker.
    try:
        STALE_MARKER.unlink()
    except OSError:
        pass
    return 0


def send_test_message() -> int:
    """--test-email: one message through _send's channels; touches no marker."""
    results = _send_channels("healthcheck test message: if you can read this, "
                             "alert delivery works.", "healthcheck test", "default")
    if not results:
        print("no channel configured (set HEALTHCHECK_NTFY_TOPIC, "
              "HEALTHCHECK_WEBHOOK or the SMTP file)")
        return 1
    for name, ok in results.items():
        print(f"{name}: {'accepted' if ok else 'FAILED'}")
    if results.get("email") is False:
        print(f"email error: {_last_email_error}")
    accepted = [n for n, ok in results.items() if ok]
    print("accepted by: " + (", ".join(accepted) if accepted else "none"))
    return 0 if accepted else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="trader.py heartbeat watchdog")
    ap.add_argument("--test-email", action="store_true",
                    help="send one test message through every configured alert "
                         "channel and exit 0 if at least one accepted it")
    # parse_known_args: an unexpected argument from some cron wrapper must not
    # turn the watchdog into an exit-2 no-op.
    args, _unknown = ap.parse_known_args()
    sys.exit(send_test_message() if args.test_email else main())
