#!/usr/bin/env python3
"""Keep the README's safety-rail table honest by deriving it from trader.py.

Every number in that table used to be typed out by hand, which is how the GitHub
profile page ended up publishing "20/20" the day MAX_POSITIONS became 24, and how
the dashboard footer used to advertise models the bot had stopped calling. A
value that is copied is a value that will eventually be wrong; the only durable
fix is to read it from the one place that defines it.

The defaults are read with `ast`, not a regex over the source text. A regex
cannot tell an active call from a commented-out one, silently takes the first of
two calls that disagree, and gives up on any default that is not a plain string
literal -- all of which mean it can publish a number the program never uses. The
parser sees the same code Python does.

This deliberately does not touch prose. Prose is the part worth writing by hand;
only the block between the RAILS markers is generated.

    python3 tools/readme_rails.py --check    # exit 1 if a README has drifted
    python3 tools/readme_rails.py --write    # regenerate the table in place
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "trader.py"
START = "<!-- RAILS:START -->"
END = "<!-- RAILS:END -->"

# (env var, English label, zh-TW label, value template; "en|zh" when they differ)
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

# Module-level constants with no env override, rendered after the env rails.
CONSTANTS = [
    ("CONFIDENCE_FLOOR_PCT", "Analyst confidence floor", "分析師信心度門檻", "{}%"),
]


class Fatal(SystemExit):
    def __init__(self, msg: str) -> None:
        super().__init__(f"readme_rails: {msg}")


def _tidy(raw: str) -> str:
    """Render a default the way a reader wants it: "3.0" -> "3", "0.50" -> "0.5".

    Only numeric-looking strings are touched. Stripping trailing zeros off
    something like "v1.0" would quietly turn it into "v1".
    """
    try:
        float(raw)
    except ValueError:
        return raw
    return raw.rstrip("0").rstrip(".") if "." in raw else raw


def _literal(node: ast.AST) -> str | None:
    """A constant node as text, or None when the value is computed at runtime."""
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    return _tidy(str(value))


def env_defaults(tree: ast.AST) -> dict[str, str]:
    """{env var: default} for every os.environ.get("NAME", <literal>) in the file.

    A second call for the same variable with a different default is an error
    rather than a silent first-wins, because there is then no single answer to
    publish and picking one would be a guess.
    """
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) != 2:
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "get"):
            continue
        owner = fn.value
        # os.environ.get(...) and environ.get(...) both reach here; anything
        # else with a .get() (a dict, a Response) must not.
        is_environ = (
            (isinstance(owner, ast.Attribute) and owner.attr == "environ")
            or (isinstance(owner, ast.Name) and owner.id == "environ")
        )
        if not is_environ:
            continue
        name = _literal(node.args[0])
        default = _literal(node.args[1])
        if name is None or default is None:
            continue
        if name in found and found[name] != default:
            raise Fatal(
                f'{name} is read twice with different defaults ({found[name]!r} and '
                f"{default!r}); there is no single value to document"
            )
        found[name] = default
    return found


def module_constants(tree: ast.AST) -> dict[str, str]:
    """{name: value} for top-level `NAME = <literal>` assignments."""
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = _literal(node.value)
        if value is not None:
            found[target.id] = value
    return found


def build_table(defaults: dict[str, str], consts: dict[str, str], zh: bool) -> str:
    head = ("| 防護機制 | 預設值 | 環境變數覆寫 |\n|------|---------|--------------|"
            if zh else
            "| Rail | Default | Env override |\n|------|---------|--------------|")
    rows = [head]

    def render(fmt: str, value: str) -> str:
        if "|" in fmt:
            fmt = fmt.split("|")[1 if zh else 0]
        return fmt.format(value)

    for var, label_en, label_zh, fmt in RAILS:
        if var not in defaults:
            raise Fatal(f'no os.environ.get("{var}", <literal>) found in {SOURCE.name}')
        rows.append(f"| {label_zh if zh else label_en} | {render(fmt, defaults[var])} | `{var}` |")

    for var, label_en, label_zh, fmt in CONSTANTS:
        if var not in consts:
            raise Fatal(f"no module-level {var} = <literal> found in {SOURCE.name}")
        rows.append(f"| {label_zh if zh else label_en} | {render(fmt, consts[var])} | "
                    f"{'—（寫死）' if zh else '— (hardcoded)'} |")
    return "\n".join(rows)


def split_on_markers(text: str, name: str) -> tuple[str, str]:
    """(before, after) around the marker block, rejecting anything ambiguous."""
    for marker in (START, END):
        count = text.count(marker)
        if count == 0:
            raise Fatal(f"{name} has no {marker}")
        if count > 1:
            raise Fatal(f"{name} has {count} copies of {marker}; it must appear exactly once")
    if text.index(START) > text.index(END):
        raise Fatal(f"{name} has {END} before {START}")
    before, rest = text.split(START, 1)
    _, after = rest.split(END, 1)
    return before, after


def apply(path: Path, table: str, write: bool) -> bool:
    """True when the file already matches; rewrites it when `write`."""
    # newline="" keeps the file's own line endings out of Python's universal
    # translation, so a --write on a CRLF checkout does not reformat every line
    # in the file and bury the real change in the diff.
    # (Path.read_text gained `newline` only in 3.13; open() has always had it.)
    with open(path, encoding="utf-8", newline="") as fh:
        raw = fh.read()
    eol = "\r\n" if "\r\n" in raw else "\n"
    text = raw.replace("\r\n", "\n")

    before, after = split_on_markers(text, path.name)
    new = f"{before}{START}\n{table}\n{END}{after}"
    if new == text:
        return True
    if write:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(new.replace("\n", eol))
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

    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    defaults = env_defaults(tree)
    consts = module_constants(tree)

    ok = True
    for path, zh in ((ROOT / "README.md", False), (ROOT / "README.zh-TW.md", True)):
        ok = apply(path, build_table(defaults, consts, zh), args.write) and ok

    if args.check:
        print("rails table matches trader.py" if ok
              else "run: python3 tools/readme_rails.py --write")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
