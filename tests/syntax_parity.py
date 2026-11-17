#!/usr/bin/env python3
"""Compares the Fe syntax dump (tools/syntax-dump) with the Rust one (fe-parser).

Runs both dumpers on each file separately, so one crash does not hide the others, and diffs
their output. Default files: every tracked .fe outside tools/, plus the toolchain's core and
std sources.

    python3 tests/syntax_parity.py --rust target/debug/examples/syntax_dump \
        --fe target/fe/fe_syntax_dump [FILE...]
"""
import argparse
import difflib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLCHAIN = os.path.expanduser("~/.femp/toolchains/fe-26.6.0/src/ingots")


def default_files():
    out = subprocess.run(
        ["git", "ls-files", "*.fe"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    files = [os.path.join(ROOT, f) for f in out if not f.startswith("tools/")]
    for ingot in ("core", "std"):
        base = os.path.join(TOOLCHAIN, ingot, "src")
        for dirpath, _, names in os.walk(base):
            files += [os.path.join(dirpath, n) for n in names if n.endswith(".fe")]
    return sorted(files)


def run(binary, path):
    try:
        done = subprocess.run(
            [binary, path], capture_output=True, text=True, timeout=120, errors="replace"
        )
    except subprocess.TimeoutExpired:
        return "<timeout>\n"
    if done.returncode != 0:
        return "<exit %d>\n%s%s" % (done.returncode, done.stdout, done.stderr)
    return done.stdout


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rust", default=os.path.join(ROOT, "target/debug/examples/syntax_dump"))
    ap.add_argument("--fe", default=os.path.join(ROOT, "target/fe/fe_syntax_dump"))
    ap.add_argument("files", nargs="*")
    args = ap.parse_args()
    files = args.files or default_files()
    failed = 0
    for path in files:
        want = run(args.rust, path)
        got = run(args.fe, path)
        if want == got:
            print("PASS", path)
            continue
        failed += 1
        print("FAIL", path)
        diff = list(
            difflib.unified_diff(
                want.splitlines(), got.splitlines(), "rust", "fe", lineterm="", n=1
            )
        )
        for line in diff[:20]:
            print("   ", line)
        if len(diff) > 20:
            print("    ... %d more diff lines" % (len(diff) - 20))
    total = len(files)
    print("%d passed, %d failed, %d total" % (total - failed, failed, total))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
