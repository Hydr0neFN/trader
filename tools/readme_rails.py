#!/usr/bin/env python3
"""Keep the README's safety-rail table honest by deriving it from trader.py.

Every number in that table used to be typed out by hand, which is how the
GitHub profile page ended up publishing "20/20" the day MAX_POSITIONS became 24,
and how the dashboard footer used to advertise models the bot had stopped
calling. A value that is copied is a value that will eventually be wrong; the
only durable fix is to read it from the one place that defines it.

This does not parse the README's prose -- prose is the part worth writing by
hand. It replaces only the block between the RAILS markers.

    python3 tools/readme_rails.py --check    # exit 1 if the table has drifted
    python3 tools/readme_rails.py --write    # regenerate it in both READMEs

Run --check before committing. --write leaves a normal diff to review.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "trader.py"
START = "<!-- RAILS:START -->"
END = "<!-- RAILS:END -->"

# (env var, English label, zh-TW label, how to render the raw default)
RAILS = [
    ("STOP_LOSS_PCT", "Hard stop-loss", "強制停損", "{}%"),
    ("TRAIL_STOP_PCT", "Trailing stop (pullback from peak)", "移動停損（自高點回檔）", "{}%"),
    ("TRAIL_ACTIVATE_PCT", "Trailing-stop activation (gain before it arms)",
     "移動停損啟動條件（啟動前所需漲幅）", "{}%"),
    ("POSITION_SIZE_PCT", "Position size", "倉位規模", "{}% of equity|淨值的 {}%"),
    ("MAX_POSITIONS", "Max concurrent positions", "最大同時持倉數", "{}"),
    ("CASH_RESERVE_USD", "Cash floor never spent", "永不動用的現金底線", "${}"),
    ("MIN_TRADE_USD", "Minimum size for a cash-trimmed order", "現金裁切後的最小下單金額", "${}"),
    ("TICKER_BATCH_SIZE", "Tickers per batch", "每批股票代碼數", "{}"),
]


def env_default(src: str, name: str) -> str:
    """The literal default in os.environ.get("NAME", "<default>")."""
    m = re.search(
        r'os\.environ\.get\(\s*["\']' + re.escape(name) + r'["\']\s*,\s*["\']([^"\']+)["\']',
        src,
    )
    if not m:
        raise SystemExit(f"readme_rails: no os.environ.get default found for {name} in {SOURCE.name}")
    return m.group(1).rstrip("0").rstrip(".") if "." in m.group(1) else m.group(1)


def confidence_floor(src: str) -> str:
    """The hardcoded analyst-confidence veto threshold."""
    m = re.search(r"gemini_confidence\s*<\s*(\d+)", src)
    if not m:
        raise SystemExit("readme_rails: confidence floor not found in " + SOURCE.name)
    return m.group(1)


def build_table(src: str, zh: bool) -> str:
    head = ("| 防護機制 | 預設值 | 環境變數覆寫 |\n|------|---------|--------------|"
            if zh else
            "| Rail | Default | Env override |\n|------|---------|--------------|")
    rows = [head]
    for var, label_en, label_zh, fmt in RAILS:
        value = env_default(src, var)
        if "|" in fmt:
            fmt_en, fmt_zh = fmt.split("|")
            shown = (fmt_zh if zh else fmt_en).format(value)
        else:
            shown = fmt.format(value)
        rows.append(f"| {label_zh if zh else label_en} | {shown} | `{var}` |")
    floor = confidence_floor(src)
    rows.append(f"| {'分析師信心度門檻' if zh else 'Analyst confidence floor'} | {floor}% | "
                f"{'—（寫死）' if zh else '— (hardcoded)'} |")
    return "\n".join(rows)


def apply(path: Path, table: str, write: bool) -> bool:
    """True when the file already matches. Prints a diff summary when it does not."""
    text = path.read_text(encoding="utf-8")
    if START not in text or END not in text:
        raise SystemExit(f"readme_rails: {path.name} has no {START} / {END} markers")
    before, rest = text.split(START, 1)
    _, after = rest.split(END, 1)
    new = f"{before}{START}\n{table}\n{END}{after}"
    if new == text:
        return True
    if write:
        path.write_text(new, encoding="utf-8", newline="\n")
        print(f"rewrote {path.name}")
    else:
        print(f"DRIFT: {path.name} does not match {SOURCE.name}")
        print(table)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="exit 1 if a README has drifted")
    g.add_argument("--write", action="store_true", help="regenerate the tables in place")
    args = ap.parse_args()

    src = SOURCE.read_text(encoding="utf-8")
    ok = True
    for path, zh in ((ROOT / "README.md", False), (ROOT / "README.zh-TW.md", True)):
        ok &= apply(path, build_table(src, zh), args.write)

    if args.check:
        print("rails table matches trader.py" if ok else "run: python3 tools/readme_rails.py --write")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
