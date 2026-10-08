"""Tests for trader_stats.py — run with `python3 -m pytest test_trader_stats.py`."""
import json
import os
from datetime import datetime, timedelta, timezone

import trader_stats as ts

EDT = timezone(timedelta(hours=-4))


def _write(path, rows, junk=True):
    with open(path, "w") as f:
        if junk:
            f.write("not json\n\n")
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _call(t, model, out, cost=0.0, cr=0, cw=0, inp=100, err=None, path="haiku-api"):
    return {"timestamp": t.isoformat(), "model": model, "path": path,
            "input_tokens": inp, "output_tokens": out, "cost_usd": cost,
            "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr, "error": err}


def _dec(t, **kw):
    return {"run_timestamp": t.isoformat(), "decision_type": "buy_analysis", **kw}


def test_default_cutover_is_1017_et():
    assert ts.CUTOVER.astimezone(ts.ET).isoformat() == "2026-10-08T10:17:00-04:00"


def test_naive_since_is_eastern_not_host_zone():
    assert ts.parse_ts("2026-10-08T10:17:00").utcoffset() == timedelta(hours=-4)
    assert ts.parse_ts("2026-12-01T10:00:00").utcoffset() == timedelta(hours=-5)  # EST
    assert ts.parse_ts("2026-10-08T22:17:00+08:00") == ts.CUTOVER


def test_window_models_cache_and_verdicts(tmp_path):
    t0 = datetime(2026, 10, 8, 10, 17, tzinfo=EDT)
    old, new = t0 - timedelta(minutes=1), t0 + timedelta(minutes=1)
    _write(tmp_path / "llm_calls.jsonl", [
        _call(old, "claude-haiku-4-5-20251001", 999),               # before cutover
        _call(new, "claude-haiku-5-5", 100, cost=0.001, cr=300, cw=100, inp=100),
        _call(new, "claude-haiku-5-5", 120, cost=0.002, err="boom"),
        _call(new, "sonnet", 50, path="sonnet-sdk"),
    ])
    _write(tmp_path / "decisions.jsonl", [
        _dec(old, haiku_verdict="VETO", final_action="VETOED"),
        _dec(new, haiku_verdict="APPROVED", final_action="BUY"),
        _dec(new, haiku_verdict="NOT_CALLED", final_action="SKIPPED_MAX_POS"),
        {"run_timestamp": new.isoformat(), "decision_type": "exit_analysis",
         "haiku_exit_verdict": "VETO", "final_exit_action": "HOLD"},
    ])
    rep = ts.build(t0, tmp_path)
    assert rep["llm_calls_rows"] == 3 and rep["decisions_rows"] == 3
    by = {m["model"]: m for m in rep["models"]}
    assert "claude-haiku-4-5-20251001" not in by
    h = by["claude-haiku-5-5"]
    assert (h["calls"], h["errors"], h["cost_usd"]) == (2, 1, 0.003)
    assert h["cache_read_tokens"] == 300 and h["cache_write_tokens"] == 100
    assert h["cache_hit_rate"] == round(300 / (200 + 100 + 300), 4)
    assert h["median_output_tokens"] == 110
    v = rep["verdicts"]
    assert v["buy"]["haiku_verdict"] == {"APPROVED": 1, "NOT_CALLED": 1}
    assert v["buy"]["final_action"] == {"BUY": 1, "SKIPPED_MAX_POS": 1}
    assert v["exit"]["haiku_exit_verdict"] == {"VETO": 1}
    assert "claude-haiku-5-5" in ts.render(rep)


def test_backward_scan_crosses_chunk_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "CHUNK", 777)  # odd size: lines straddle chunk edges
    t0 = datetime(2026, 10, 8, 10, 0, tzinfo=EDT)
    rows = [_call(t0 + timedelta(seconds=i), "m", i) for i in range(3000)]
    _write(tmp_path / "llm_calls.jsonl", rows, junk=False)
    got = list(ts.iter_since(tmp_path / "llm_calls.jsonl", t0 + timedelta(seconds=10), "timestamp"))
    assert [r["output_tokens"] for r in got] == list(range(10, 3000))  # ordered, none lost


def test_read_only_and_missing_files(tmp_path):
    p = tmp_path / "llm_calls.jsonl"
    _write(p, [_call(datetime(2026, 10, 9, tzinfo=EDT), "m", 1)])
    p.chmod(0o444)
    before = p.read_bytes()
    rep = ts.build(ts.CUTOVER, tmp_path)          # decisions.jsonl absent: no crash
    assert rep["decisions_rows"] == 0 and rep["llm_calls_rows"] == 1
    assert p.read_bytes() == before
    assert not (tmp_path / "trader.lock").exists()
    assert set(os.listdir(tmp_path)) == {"llm_calls.jsonl"}  # nothing created
