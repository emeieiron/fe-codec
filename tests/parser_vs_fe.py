#!/usr/bin/env python3
"""Checks the Fe syntax parser (tools/syntax, via tools/syntax-dump) against the official Fe
compiler (`fe check`) by mutation testing. Standard library only; nothing is stored.

    python3 tests/parser_vs_fe.py [--mutants N] [--seed S] [--keep] [--jobs J]

Build the dumper first (see tools/README.md):
    fe build --backend native tools/syntax-dump --out-dir target/fe

1. Acceptance of real files (no oracle): the parser must accept every tracked .fe file (except
   tests/generator_corpus/syntax_* cases and cases marked `syntax_error = true`) and every file of
   the toolchain's core and std ingots.
2. Agreement on seed files: declaration-only seeds (corpus sources, examples' models, ~10
   synthesized files) must be accepted by both `fe check` (no `error[1-...]`) and the parser.
3. Mutants: each seed is lexed (see `lex` below), ONE random mutation is applied to the token
   stream, and both tools judge the result. Results are classified as
     agree-accept, agree-reject-same-location, agree-reject-different-location,
     parser-accepts-fe-rejects, parser-rejects-fe-accepts.
   Positions are (line, column), 1-based; line = 1 + number of "\\n" before the offset; column =
   characters (fe check's unit, verified by a probe at startup) since the last "\\n", plus 1.
   fe check's position is the lowest one among the `error[1-` diagnostics it prints.

Mutation kinds: delete-token, duplicate-token, swap-adjacent, insert-token (from a pool),
delete-newline, newline-to-space, insert-newline (a newline plus optional indentation at a
random gap between tokens).

What is not mutated (the parser skips these by design and checks nothing inside them):
  * every token of an item that is not `use`, `struct`, `enum` or `impl` (fn, trait, mod,
    contract, const, type, static_assert, extern items, with their attributes and docs),
  * the interior of an impl body and its closing brace (the opening brace and the header are
    mutated),
  * the predicates of a `where` clause (the parser tells a type bound from a const condition
    by looking for `:` and skips const conditions as a token run; `where` itself and the `{`
    after the clause are mutated),
  * the interior of attribute arguments `#[name( ... )]` and of attribute values `#[name = ...]`,
    and the interior of array lengths `[T; ...]`: the parser skips these by bracket balance and
    never parses the expressions in them (the delimiters themselves are mutated, and nothing is
    inserted just before an opening or closing delimiter of such a region (it would become the
    delimiter or land inside), and a delimiter is never swapped; nothing is appended to a skipped `const`/`type` line).
Plain `//` comments are never mutated; `///` doc comments are, because both parsers treat them
as attributes. A mutation that deletes or replaces a newline is skipped when the next non-blank
token is a comment or the end of the file (it would touch only comments).

Because inserted tokens can create a skipped construct (`fn`, `trait`, ... at the start of an
item), `parser-accepts-fe-rejects` is split: if fe's error position lies inside an item the
parser reports as `other` (or inside an impl body), it is counted as
`excused-skipped-construct` and does not fail the run; so is a difference (accept/reject or
location) whose fe error lies inside the braces of an `impl`, or inside a `( ... )` call or `{ ... }` block
in a const generic argument (`Text<f(...)>`), neither of which the parser looks into.
Everything else fails the run.

Tokenizer (`lex`): a Python port of tools/syntax/src/lexer.fe over bytes. Ident, Int (with
0b/0o/0x prefixes and `_` between digits), Str, Byte (b'..'), `//` and `///` comments up to the
line end, runs of \\r/\\n (Newline), runs of space/tab (Whitespace), `::` `->` `=>` `..` and
`** && || == !=` as one token, single punctuation otherwise, and every other byte Unknown (runs
of non-ASCII bytes are merged so a mutation never splits a UTF-8 character).
"""
import argparse
import collections
import concurrent.futures
import glob
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DUMP = os.path.join(ROOT, "target/fe/fe_syntax_dump")
FE_ENV = dict(os.environ, NO_COLOR="1")
FE_ENV["PATH"] = os.path.expanduser("~/.fe/bin") + os.pathsep + FE_ENV.get("PATH", "")


def _find_toolchain():
    """The `ingots` directory (core and std sources) of the Fe the `fe` on PATH was built from:
    FE_TOOLCHAIN_INGOTS if set, else FeMP's managed toolchain for that version
    (~/.femp/toolchains/fe-<version>/src/ingots), else a Cargo checkout of the commit the
    binary reports (~/.cargo/git/checkouts/fe-*/<commit>/ingots)."""
    override = os.environ.get("FE_TOOLCHAIN_INGOTS")
    if override:
        return override
    out = subprocess.run(["fe", "--version"], capture_output=True, text=True, env=FE_ENV).stdout
    match = re.search(r"(\d+\.\d+\.\d+) \((\w+)\)", out)
    if not match:
        sys.exit("cannot read the version from `fe --version`: %r" % out)
    version, commit = match.groups()
    candidates = [os.path.expanduser("~/.femp/toolchains/fe-%s/src/ingots" % version)]
    candidates += sorted(glob.glob(os.path.expanduser("~/.cargo/git/checkouts/fe-*/%s*/ingots" % commit)))
    for path in candidates:
        if os.path.isdir(os.path.join(path, "core", "src")):
            return path
    sys.exit("no sources of Fe %s (%s) found; set FE_TOOLCHAIN_INGOTS to its `ingots` directory"
             % (version, commit))


TOOLCHAIN = _find_toolchain()

# ---------------------------------------------------------------------------------------------
# Lexer

IDENT, INT, STR, BYTE, PUNCT, NEWLINE, WS, LCOMMENT, DOC, UNKNOWN = (
    "ident int str byte punct newline ws comment doc unknown".split()
)
SINGLE = set(b"(){}[]:;.,#+-*/%~!?^&|<>=")
DOUBLE = {b"::", b"->", b"=>", b"..", b"**", b"&&", b"||", b"==", b"!="}


def _ident_start(c):
    return 97 <= c <= 122 or 65 <= c <= 90 or c == 95


def _digit(c):
    return 48 <= c <= 57


def lex(src):
    """Returns [(kind, start, end)] covering every byte of `src` (bytes)."""
    n = len(src)
    out = []
    pos = 0
    while pos < n:
        b = src[pos]
        nxt = src[pos + 1] if pos + 1 < n else 0
        i = pos + 1
        kind = UNKNOWN
        if b in (10, 13):
            while i < n and src[i] in (10, 13):
                i += 1
            kind = NEWLINE
        elif b in (32, 9):
            while i < n and src[i] in (32, 9):
                i += 1
            kind = WS
        elif b == 47 and nxt == 47:
            kind = DOC if pos + 2 < n and src[pos + 2] == 47 else LCOMMENT
            while i < n and src[i] not in (10, 13):
                i += 1
        else:
            end = None
            if b == 98 and nxt == 39:  # b'
                j = pos + 2
                while j < n:
                    c = src[j]
                    if c == 39:
                        end = j + 1
                        break
                    if c in (10, 13):
                        break
                    if c == 92:
                        if j + 1 >= n or src[j + 1] in (10, 13):
                            break
                        j += 2
                    else:
                        j += 1
            if end is not None:
                kind, i = BYTE, end
            elif _ident_start(b):
                while i < n and (_ident_start(src[i]) or _digit(src[i])):
                    i += 1
                kind = IDENT
            elif _digit(b):
                kind, i = INT, _int_end(src, pos)
            elif b == 34:
                j = pos + 1
                end = None
                while j < n:
                    c = src[j]
                    if c == 34:
                        end = j + 1
                        break
                    if c == 92:
                        if j + 1 >= n or src[j + 1] == 10:
                            break
                        j += 2
                    else:
                        j += 1
                if end is not None:
                    kind, i = STR, end
            elif src[pos:pos + 2] in DOUBLE:
                kind, i = PUNCT, pos + 2
            elif b in SINGLE:
                kind = PUNCT
            elif b >= 0x80:
                while i < n and 0x80 <= src[i] < 0xC0:
                    i += 1
        out.append((kind, pos, i))
        pos = i
    return out


def _int_end(src, start):
    n = len(src)
    nxt = src[start + 1] if start + 1 < n else 0
    if src[start] == 48:
        i = start + 2
        if nxt in b"bB":
            while i < n and src[i] in b"01":
                i += 1
            if i > start + 2:
                return i
        elif nxt in b"oO":
            while i < n and src[i] in b"01234567":
                i += 1
            if i > start + 2:
                return i
        elif nxt in b"xX":
            while i < n and src[i] in b"0123456789abcdefABCDEF":
                i += 1
            if i > start + 2:
                return i
    i = start
    while i < n and _digit(src[i]):
        i += 1
    while i + 1 < n and src[i] == 95 and _digit(src[i + 1]):
        i += 1
        while i < n and _digit(src[i]):
            i += 1
    return i


def is_comment(kind):
    return kind in (LCOMMENT, DOC)


def is_trivia(kind):
    return kind in (WS, NEWLINE, LCOMMENT, DOC)


# ---------------------------------------------------------------------------------------------
# Which tokens may be mutated

SKIPPED_ITEMS = {"fn", "trait", "mod", "contract", "msg", "extern", "const", "type", "static_assert"}


def mutable_mask(src, toks):
    """True for each token that may be mutated (see the module docstring)."""
    n = len(toks)

    def text(k):
        return src[toks[k][1]:toks[k][2]] if toks[k][0] == PUNCT else b""

    mask = [True] * n
    no_insert = set()

    def sig_from(k):
        while k < n and is_trivia(toks[k][0]):
            k += 1
        return k

    def brace_end(k):
        """Index just past the first `{...}` at or after k (outside parens), or None."""
        depth = 0
        while k < n:
            t = text(k)
            if t in (b"(", b"["):
                depth += 1
            elif t in (b")", b"]"):
                depth -= 1
            elif t == b"{" and depth <= 0:
                d = 0
                while k < n:
                    tt = text(k)
                    if tt == b"{":
                        d += 1
                    elif tt == b"}":
                        d -= 1
                        if d == 0:
                            return k + 1
                    k += 1
                return n
            k += 1
        return None

    def line_end(k):
        depth = 0
        while k < n:
            t = text(k)
            if t in (b"(", b"[", b"{"):
                depth += 1
            elif t in (b")", b"]", b"}"):
                depth -= 1
            elif toks[k][0] == NEWLINE and depth <= 0:
                return k
            k += 1
        return n

    i = 0
    while i < n:
        j = sig_from(i)
        if j >= n:
            break
        start = i
        # Head of an item: attributes, `pub`, `pub(...)`, `unsafe`; docs are trivia here.
        while j < n:
            j = sig_from(j)
            if j >= n:
                break
            t = text(j)
            if t == b"#" and j + 1 < n and text(j + 1) == b"[":
                d = 0
                j += 1
                while j < n:
                    tt = text(j)
                    if tt == b"[":
                        d += 1
                    elif tt == b"]":
                        d -= 1
                        if d == 0:
                            break
                    j += 1
                j += 1
            elif toks[j][0] == IDENT and src[toks[j][1]:toks[j][2]] in (b"pub", b"unsafe"):
                is_pub = src[toks[j][1]:toks[j][2]] == b"pub"
                j += 1
                if is_pub:
                    p = j
                    while p < n and toks[p][0] == WS:
                        p += 1
                    if p < n and text(p) == b"(":
                        while p < n and text(p) != b")":
                            p += 1
                        j = p + 1
            else:
                break
        if j >= n:
            break
        kw = src[toks[j][1]:toks[j][2]].decode("latin1") if toks[j][0] == IDENT else ""
        if kw == "use":
            end = line_end(j)
        elif kw in ("struct", "enum"):
            e = brace_end(j)
            end = e if e is not None else line_end(j)
        elif kw == "impl":
            e = brace_end(j)
            if e is None:
                end = line_end(j)
            else:
                o = j
                depth = 0
                while o < e:
                    t = text(o)
                    if t in (b"(", b"["):
                        depth += 1
                    elif t in (b")", b"]"):
                        depth -= 1
                    elif t == b"{" and depth <= 0:
                        break
                    o += 1
                for q in range(o + 1, e):
                    mask[q] = False
                end = e
        elif kw in SKIPPED_ITEMS:
            if kw in ("const", "type", "static_assert"):
                end = line_end(j)
                no_insert.add(end)  # appending to the line would extend the skipped item
            else:
                e = brace_end(j)
                end = e if e is not None else line_end(j)
            for q in range(start, end):
                mask[q] = False
        else:
            end = j + 1  # not an item: mutable glue
        if kw in ("struct", "enum", "impl"):
            # `where` predicates: the parser tells type bounds from const conditions by looking
            # for a `:` and skips the latter as a token run, so the clause is left alone.
            o, depth, w = j, 0, None
            while o < end:
                t = text(o)
                if t in (b"(", b"["):
                    depth += 1
                elif t in (b")", b"]"):
                    depth -= 1
                elif t == b"{" and depth <= 0:
                    break
                elif toks[o][0] == IDENT and src[toks[o][1]:toks[o][2]] == b"where" and depth <= 0:
                    w = o
                o += 1
            if w is not None and o < end:
                for q in range(w + 1, o):
                    mask[q] = False
                    no_insert.add(q)
                no_insert.add(o)
        i = max(end, start + 1)
    close_of = {b"(": b")", b"[": b"]", b"{": b"}"}

    def match(k):
        """Index of the bracket closing the one at k, or n."""
        depth = 0
        while k < n:
            t = text(k)
            if t in close_of:
                depth += 1
            elif t in (b")", b"]", b"}"):
                depth -= 1
                if depth == 0:
                    return k
            k += 1
        return n

    def skip_interior(a, b):
        """Tokens a..b-1 are interior; the token at b is the closer."""
        for q in range(a, min(b, n)):
            mask[q] = False
            no_insert.add(q)
        if b < n:
            no_insert.add(b)

    def back(k):
        """The first of the blanks right before token k (inserting there would come first)."""
        while k > 0 and toks[k - 1][0] == WS:
            k -= 1
        return k

    stack = []
    for k in range(n):
        t = text(k)
        if t == b"[":
            prev = k - 1
            attr = prev >= 0 and (text(prev) == b"#" or (text(prev) == b"!" and prev > 0 and text(prev - 1) == b"#"))
            stack.append((k, attr))
            if attr:
                e = match(k)
                j = k + 1
                while j < e and (toks[j][0] in (IDENT, WS) or text(j) == b"::"):
                    j += 1
                if j < e and text(j) == b"(":
                    no_insert.update(range(back(j), j + 1))
                    skip_interior(j + 1, match(j))
                elif j < e and text(j) == b"=":
                    no_insert.update(range(back(j), j + 1))
                    skip_interior(j + 1, e)
        elif t == b"]":
            if stack:
                stack.pop()
        elif t == b";" and stack and not stack[-1][1]:
            no_insert.update(range(back(k), k + 1))
            skip_interior(k + 1, match(stack[-1][0]))
    return mask, no_insert


# ---------------------------------------------------------------------------------------------
# Mutations

POOL = ["{", "}", "(", ")", "[", "]", "<", ">", ",", ":", ";", "=", "#", "@", "!", ".", "::",
        "->", "pub", "struct", "enum", "use", "fn", "where", "0", "x", '"s"', "\n"]
KINDS = ["delete-token", "duplicate-token", "swap-adjacent", "insert-token", "delete-newline",
         "newline-to-space", "insert-newline"]


def next_nonblank(toks, k):
    k += 1
    while k < len(toks) and toks[k][0] in (WS, NEWLINE):
        k += 1
    return toks[k][0] if k < len(toks) else None


def splice(src, start, end, new):
    return src[:start] + new + src[end:]


def adds_only(src, pos, new, ntoks):
    """Whether inserting `new` at `pos` adds exactly the tokens of `new` (nothing merges)."""
    return len(lex(splice(src, pos, pos, new))) == ntoks + len(lex(new))


def mutate(rng, src, toks, region, kind):
    """Returns (mutant_bytes, description, byte position) or None if the kind does not apply."""
    n = len(toks)
    mask, no_insert = region

    def text(t):
        return src[t[1]:t[2]]

    def show(b):
        return repr(b.decode("utf8", "replace"))

    plain = [k for k in range(n) if mask[k] and toks[k][0] not in (LCOMMENT, NEWLINE, WS)]
    if kind == "delete-token":
        if not plain:
            return None
        t = toks[rng.choice(plain)]
        return splice(src, t[1], t[2], b""), "delete " + show(text(t)), t[1]
    if kind == "duplicate-token":
        if not plain:
            return None
        # The copy lands just before the next token, which must be a valid insertion point.
        ok = [k for k in plain if k + 1 >= n or (mask[k + 1] and k + 1 not in no_insert)]
        if not ok:
            return None
        t = toks[rng.choice(ok)]
        new = text(t) if adds_only(src, t[2], text(t), n) else b" " + text(t)
        return splice(src, t[2], t[2], new), "duplicate " + show(text(t)), t[1]
    if kind == "swap-adjacent":
        c = []
        for a, b in zip(plain, plain[1:]):
            if (all(is_trivia(toks[q][0]) for q in range(a + 1, b)) and text(toks[a]) != text(toks[b])
                    and a not in no_insert and b not in no_insert):
                c.append((a, b))
        if not c:
            return None
        a, b = rng.choice(c)
        ta, tb = toks[a], toks[b]
        m = src[:ta[1]] + text(tb) + src[ta[2]:tb[1]] + text(ta) + src[tb[2]:]
        return m, "swap %s %s" % (show(text(ta)), show(text(tb))), ta[1]
    if kind == "insert-token":
        c = [k for k in range(n) if mask[k] and k not in no_insert] + ([n] if n and mask[n - 1] else [])
        if not c:
            return None
        k = rng.choice(c)
        pos = toks[k][1] if k < n else len(src)
        new = rng.choice(POOL).encode()
        ins = new if adds_only(src, pos, new, n) else b" " + new + b" "
        return splice(src, pos, pos, ins), "insert " + show(new), pos
    if kind == "insert-newline":
        c = [k for k in range(n) if mask[k] and k not in no_insert and toks[k][0] != WS]
        if not c:
            return None
        pos = toks[rng.choice(c)][1]
        new = b"\n" + b" " * rng.choice([0, 0, 2, 4, 8])
        return splice(src, pos, pos, new), "insert newline" + (" + indent %d" % (len(new) - 1) if len(new) > 1 else ""), pos
    if kind in ("delete-newline", "newline-to-space"):
        c = []
        for k in range(n):
            if mask[k] and toks[k][0] == NEWLINE:
                nb = next_nonblank(toks, k)
                if nb is not None and not is_comment(nb):
                    c.append(k)
        if not c:
            return None
        t = toks[rng.choice(c)]
        e = t[1] + (2 if text(t).startswith(b"\r\n") else 1)  # one newline of the run
        new = b"" if kind == "delete-newline" else b" "
        return splice(src, t[1], e, new), ("delete newline" if not new else "newline -> space"), t[1]
    raise ValueError(kind)


# ---------------------------------------------------------------------------------------------
# The two judges

class Workdir:
    """A scratch ingot per thread."""

    def __init__(self, base):
        self.base = base
        self.local = threading.local()
        self.count = 0
        self.lock = threading.Lock()

    def get(self):
        d = getattr(self.local, "dir", None)
        if d is None:
            with self.lock:
                self.count += 1
                d = os.path.join(self.base, "ingot%d" % self.count)
            os.makedirs(os.path.join(d, "src"))
            with open(os.path.join(d, "fe.toml"), "w") as f:
                f.write('[ingot]\nname = "p"\nversion = "0.1.0"\n')
            self.local.dir = d
        return d


WORK = None
FE_ERR = re.compile(r"error\[(1-\d+)\]: ([^\n]*)\n\s*┌─ [^\n]*?:(\d+):(\d+)")


def fe_check(src):
    """Returns (errors, problem): errors = [(line, col, code, message)] of every `error[1-`
    diagnostic in the order printed; problem is a string if fe check could not be judged."""
    d = WORK.get()
    with open(os.path.join(d, "src", "lib.fe"), "wb") as f:
        f.write(src)
    try:
        done = subprocess.run(["fe", "check", d], capture_output=True, text=True, timeout=120,
                              env=FE_ENV, errors="replace")
    except subprocess.TimeoutExpired:
        return [], "fe check timed out"
    out = done.stdout + done.stderr
    errs = [(int(m.group(3)), int(m.group(4)), m.group(1), m.group(2)) for m in FE_ERR.finditer(out)]
    if out.count("error[1-") != len(errs):
        return errs, "could not read every parse error in: " + out[:300]
    if done.returncode not in (0, 1, 2) and "error[" not in out:
        return errs, "fe check exit %d: %s" % (done.returncode, out[:200])
    return errs, None


def run_dump(path):
    """Runs the parser dumper; returns (offset, None, or "crash", output)."""
    done = subprocess.run([DUMP, path], capture_output=True, text=True, timeout=120, errors="replace")
    if done.returncode != 0:
        return "crash", "exit %d: %s%s" % (done.returncode, done.stdout, done.stderr)
    m = re.search(r"^error (\d+)$", done.stdout, re.M)
    return (int(m.group(1)) if m else None), done.stdout


def parse_mutant(src):
    path = os.path.join(WORK.get(), "dump.fe")
    with open(path, "wb") as f:
        f.write(src)
    return run_dump(path)


def pos_of(src, offset, chars):
    """(line, column) of byte `offset`: 1-based; the column counts characters or bytes."""
    offset = min(offset, len(src))
    line = 1 + src.count(b"\n", 0, offset)
    seg = src[src.rfind(b"\n", 0, offset) + 1:offset]
    col = (sum(1 for b in seg if (b & 0xC0) != 0x80) if chars else len(seg)) + 1
    return line, col


def offset_of(src, line, col, chars):
    """Inverse of pos_of: the byte offset of (line, col)."""
    start = 0
    for _ in range(line - 1):
        nl = src.find(b"\n", start)
        if nl < 0:
            return len(src)
        start = nl + 1
    if not chars:
        return min(start + col - 1, len(src))
    i, seen = start, 0
    while i < len(src) and seen < col - 1:
        i += 1
        while i < len(src) and (src[i] & 0xC0) == 0x80:
            i += 1
        seen += 1
    return i


def dump_items(out):
    return [(m.group(1), int(m.group(2))) for m in
            (re.match(r"^(use|struct|enum|impl_trait|other) (\d+)\b", l) for l in out.splitlines()) if m]


def excused(src, fe_pos, dump_out, chars):
    """Whether fe's error lies in a construct the parser skips by design."""
    off = offset_of(src, fe_pos[0], fe_pos[1], chars)
    items = dump_items(dump_out)
    for idx, (kind, start) in enumerate(items):
        end = items[idx + 1][1] if idx + 1 < len(items) else len(src) + 1
        if start <= off < end:
            if kind == "other":
                return True
            if kind == "impl_trait":
                brace = src.find(b"{", start)
                return 0 <= brace < off
            return False
    return False


def in_impl_body(src, off):
    """Whether byte `off` is inside the `{ ... }` of a top-level `impl` item (to the end of the
    input if it is not closed); the parser skips those by bracket balance."""
    depth, pending, body = 0, False, None
    for kind, a, b in lex(src):
        t = src[a:b]
        if depth == 0 and kind == IDENT and t == b"impl":
            pending = True
        elif kind == PUNCT and t in (b"{", b"(", b"["):
            if depth == 0 and t == b"{" and pending:
                body = a
                pending = False
            depth += 1
        elif kind == PUNCT and t in (b"}", b")", b"]"):
            depth = max(depth - 1, 0)
            if depth == 0 and body is not None:
                if body < off <= a:
                    return True
                body = None
    return body is not None and body < off


def in_generic_call(src, off):
    """Whether byte `off` is inside a `( ... )` group (to the end of the input if it is not
    closed) or `{ ... }` block that opens while an angle-bracket list is open: a call or block in a
    const generic argument or default, `Text<f(...)>`, `Text<{ N }>`, `= [1, 2]`. The parser skips such groups by bracket balance. Angle brackets
    are counted as in `lex` terms: a `<` right after an identifier opens a list, `>` closes."""
    angle, stack, prev = 0, [], None
    for kind, a, b in lex(src):
        t = src[a:b]
        if kind in (WS, NEWLINE, LCOMMENT, DOC):
            continue
        if kind == PUNCT:
            if t == b"<" and prev is not None and prev[0] == IDENT:
                angle += 1
            elif t == b">":
                angle = max(angle - 1, 0)
            elif t in (b"(", b"{"):
                stack.append((a, angle > 0))
            elif t == b"[":
                # An array literal or an index: after `=` or a value, not a type.
                stack.append((a, angle > 0 and prev is not None and (prev[1] == b"=" or prev[0] == INT)))
            elif t in (b")", b"}", b"]") and stack:
                start, generic = stack.pop()
                if generic and start < off <= a:
                    return True
        prev = (kind, t)
    return any(g and st < off for st, g in stack)


def detect_chars():
    """Probe: does fe check count columns in characters (True) or bytes (False)?"""
    errs, bad = fe_check('#[a("éé")] pub struct A { x: u8 y: u8 }\n'.encode())
    if bad or not errs:
        sys.exit("column probe failed: %s %s" % (bad, errs))
    # The error is at the space after `]`: character column 11, byte column 13.
    if errs[0][1] in (11, 13):
        return errs[0][1] == 11
    sys.exit("column probe gave an unexpected column %d" % errs[0][1])


# ---------------------------------------------------------------------------------------------
# Seeds

SYNTH = {
    "synth_generics": """\
use core::marker::Copy
pub struct Pair<A, B> {
    pub first: A,
    pub second: B,
}
pub struct Bounded<T: Copy + Eq, const N: usize> {
    pub items: [T; N],
    pub extra: Option<Pair<T, u8>>,
}
pub enum Choice<T> where T: Copy {
    Nothing,
    Some(T),
    Both { left: T, right: T },
}
""",
    "synth_attributes": """\
#[serializable]
#[codec(id = 1)]
pub struct Tagged {
    #[codec(id = 0x01)]
    pub a: u32,
    #[codec(id = 2)]
    pub b: [u8; 4],
}

#[derive(Eq)]
enum Mode {
    #[codec(id = 1)]
    Fast,
    #[codec(id = 2)]
    Slow(u8),
}
""",
    "synth_docs": """\
/// A record with docs.
///
/// Second paragraph.
pub struct Documented {
    /// The id.
    pub id: u64,
    // a plain comment
    /// The label.
    pub label: String<16>,
}

/// An enum with docs.
pub enum Level {
    /// Low.
    Low,
    /// High.
    High,
}
""",
    "synth_visibility": """\
pub(ingot) struct Inner {
    pub(ingot) value: u8,
    pub(super) other: u16,
    hidden: u32,
}
pub(super) enum Scope {
    A,
    B,
}
""",
    "synth_enums": """\
pub enum Shape {
    Point,
    Circle(u32),
    Rect { w: u32, h: u32 },
    Pair(u8, u8),
}
enum Empty {}
pub enum Number {
    Zero,
    One,
    Two,
}
""",
    "synth_marker_impls": """\
pub struct Wrapper { pub inner: u8 }
impl Copy for Wrapper {}
impl Eq for Wrapper {}
pub struct Other<T> { pub t: T }
impl<T> Copy for Other<T> {}
impl Wrapper {
    pub fn get(self) -> u8 {
        self.inner
    }
}
""",
    "synth_use_trees": """\
use core::{
    MemBuffer,
    MemVec,
    Option,
    Result,
}
use std::io::{Stderr, Stdout, print_buffer}
use ingot::models::{
    Item,
    Kind as K,
    nested::{A, B},
}
use super::sibling
pub use core::marker::Copy
""",
    "synth_types": """\
pub struct Types {
    pub a: [u8; 32],
    pub b: [[u8; 4]; 2],
    pub c: (u8, u16),
    pub d: Option<[u8; 0x10]>,
    pub e: ingot::other::Thing,
    pub f: Vec<Option<u8>>,
    pub g: [u8; 1_000],
    pub h: *u8,
    pub i: (),
    pub j: (u8,),
    pub k: (u8),
    pub l: Option<(u8, (bool, u16),)>,
    pub m: [(u8, bool); 2],
}
""",
    "synth_mixed": """\
use core::marker::Copy

const LIMIT: u32 = 10
type Alias = u8

/// A record.
#[serializable]
pub struct Rec {
    pub id: u32,
    pub tag: Alias,
}

pub trait Named {
    fn name(self) -> u8
}

impl Named for Rec {
    fn name(self) -> u8 {
        self.id as u8
    }
}

pub fn helper(x: u8) -> u8 {
    x + 1
}

pub enum Kind {
    A,
    B,
}
""",
    "synth_tight": """\
use a::b
pub struct T<A,B>{pub x:A,pub y:[B;2]}
enum E{A,B(u8),C{z:u8}}
impl<A> Copy for T<A,u8>{}
""",
}


def read_case_toml(path):
    text = open(path, encoding="utf8").read()
    flag = re.search(r"^syntax_error\s*=\s*true", text, re.M) is not None
    m = re.search(r"^sources\s*=\s*\[(.*?)\]", text, re.M | re.S)
    return flag, (re.findall(r'"([^"]+)"', m.group(1)) if m else [])


def corpus_cases():
    """[(name, malformed, sources)] for every corpus case."""
    corpus = os.path.join(ROOT, "tests/generator_corpus")
    out = []
    for name in sorted(os.listdir(corpus)):
        toml = os.path.join(corpus, name, "case.toml")
        if os.path.exists(toml):
            flag, sources = read_case_toml(toml)
            out.append((name, flag or name.startswith("syntax_"), sources))
    return out


def real_files():
    bad = ["tests/generator_corpus/%s/" % name for name, malformed, _ in corpus_cases() if malformed]
    tracked = subprocess.run(["git", "ls-files", "*.fe"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.split()
    files = [os.path.join(ROOT, f) for f in tracked if not any(f.startswith(b) for b in bad)]
    for ingot in ("core", "std"):
        for dirpath, _, names in os.walk(os.path.join(TOOLCHAIN, ingot, "src")):
            files += [os.path.join(dirpath, n) for n in names if n.endswith(".fe")]
    return sorted(files)


def seed_files():
    """[(name, bytes)]"""
    seeds = []
    for name, malformed, sources in corpus_cases():
        if malformed:
            continue
        for s in sources:
            p = os.path.join(ROOT, "tests/generator_corpus", name, s)
            if os.path.exists(p):
                seeds.append(("corpus/%s/%s" % (name, s), open(p, "rb").read()))
    for rel in ("examples/vanilla/src/models.fe", "examples/transfer/shared/src/models.fe",
                "examples/transfer/shared/src/typed.fe"):
        seeds.append((rel, open(os.path.join(ROOT, rel), "rb").read()))
    seeds += [(name, text.encode()) for name, text in SYNTH.items()]
    return seeds


# ---------------------------------------------------------------------------------------------
# Classification and reporting

CLASSES = ["agree-accept", "agree-reject-same-location", "agree-reject-different-location",
           "parser-accepts-fe-rejects", "excused-skipped-construct", "parser-rejects-fe-accepts"]
EXTRA = ["parser-crash", "oracle-failure"]  # shown when they occur
FAILING = {"agree-reject-different-location", "parser-accepts-fe-rejects", "parser-rejects-fe-accepts",
           "parser-crash", "oracle-failure"}


def classify(parser_off, fe_errs, src, chars):
    """Returns (class, parser_pos, fe_pos); fe's position is the lowest `error[1-` printed."""
    ppos = pos_of(src, parser_off, chars) if isinstance(parser_off, int) else None
    fpos = min((e[0], e[1]) for e in fe_errs) if fe_errs else None
    if ppos is None and fpos is None:
        return "agree-accept", ppos, fpos
    if ppos is None:
        return "parser-accepts-fe-rejects", ppos, fpos
    if fpos is None:
        return "parser-rejects-fe-accepts", ppos, fpos
    return ("agree-reject-same-location" if ppos == fpos else "agree-reject-different-location"), ppos, fpos


def verdict(src, chars):
    """Runs both judges on `src`: a dict with class, ppos, fpos, fe_errs, signature, note."""
    off, out = parse_mutant(src)
    errs, problem = fe_check(src)
    v = {"fe_errs": errs, "ppos": None, "fpos": None, "sig": "", "note": ""}
    if off == "crash":
        v.update({"class": "parser-crash", "note": out, "sig": "parser crash: " + out.strip().splitlines()[-1][:60]})
        if errs:
            v["fpos"] = min((e[0], e[1]) for e in errs)
        return v
    if problem:
        v.update({"class": "oracle-failure", "note": problem, "sig": "oracle-failure"})
        return v
    cls, ppos, fpos = classify(off, errs, src, chars)
    if cls == "parser-accepts-fe-rejects" and excused(src, fpos, out, chars):
        cls = "excused-skipped-construct"
    elif (cls in ("parser-accepts-fe-rejects", "agree-reject-different-location")
          and (in_impl_body(src, offset_of(src, fpos[0], fpos[1], chars))
               or in_generic_call(src, offset_of(src, fpos[0], fpos[1], chars)))):
        cls = "excused-skipped-construct"
    v.update({"class": cls, "ppos": ppos, "fpos": fpos})
    msg = min(errs, key=lambda e: (e[0], e[1]))[3] if errs else ""
    sig = msg
    if cls == "agree-reject-different-location":
        # Does fe sit at the end of the token before the parser's token (start of the blanks
        # that precede it)?
        poff = offset_of(src, ppos[0], ppos[1], chars)
        a = poff
        while a > 0 and src[a - 1] in b" \t":
            a -= 1
        foff = offset_of(src, fpos[0], fpos[1], chars)
        sig += " | " + ("fe at the start of the blanks before the parser's token" if foff == a
                        else "fe %s the parser's token" % ("before" if foff < poff else "after"))
    v["sig"] = sig
    return v


def line_of(src, line):
    lines = src.split(b"\n")
    return lines[line - 1].decode("utf8", "replace") if 0 < line <= len(lines) else ""


def show_example(ex):
    a, b = ex["orig"].split(b"\n"), ex["src"].split(b"\n")
    out = ["  seed %s: %s (byte %d)" % (ex["seed"], ex["desc"], ex["pos"])]
    shown = 0
    for i in range(max(len(a), len(b))):
        la = a[i].decode("utf8", "replace") if i < len(a) else "<gone>"
        lb = b[i].decode("utf8", "replace") if i < len(b) else "<gone>"
        if la != lb and shown < 4:
            out += ["    - %d: %s" % (i + 1, la), "    + %d: %s" % (i + 1, lb)]
            shown += 1
    out.append("    parser: %s    fe check: %s%s" % (ex["ppos"] or "parses", ex["fpos"] or "parses",
                                                  "  (" + ex["sig"].split(" | ")[0] + ")" if ex["sig"] else ""))
    if ex["ppos"]:
        out.append("    parser error line %d: %s" % (ex["ppos"][0], line_of(ex["src"], ex["ppos"][0])))
    if ex["fpos"]:
        out.append("    fe error line     %d: %s" % (ex["fpos"][0], line_of(ex["src"], ex["fpos"][0])))
    return "\n".join(out)


def reduce_input(src, cls, sig, chars):
    """Shrinks `src` while both judges keep giving the same class and signature: whole lines
    first, then single tokens, until nothing more can be removed."""
    def same(candidate):
        v = verdict(candidate, chars)
        return v["class"] == cls and v["sig"] == sig

    lines = src.split(b"\n")
    changed = True
    while changed:
        changed = False
        for i in range(len(lines) - 1, -1, -1):
            cand = lines[:i] + lines[i + 1:]
            if cand and same(b"\n".join(cand)):
                lines, changed = cand, True
    src = b"\n".join(lines)
    changed = True
    while changed:
        changed = False
        toks = lex(src)
        for k in range(len(toks) - 1, -1, -1):
            cand = src[:toks[k][1]] + src[toks[k][2]:]
            if len(lex(cand)) != len(toks) - 1:
                continue  # the neighbours would merge into one token
            if cand and same(cand):
                src, changed = cand, True
                toks = lex(src)
    return src


def main():
    global WORK, DUMP
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mutants", type=int, default=400)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--keep", action="store_true", help="keep the scratch directory and the failing mutants")
    ap.add_argument("--reduce", action="store_true", help="shrink one example of each disagreement signature")
    ap.add_argument("--dump", default=DUMP, help="the fe_syntax_dump binary to test")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4, help="parallel fe check runs")
    args = ap.parse_args()
    t0 = time.time()
    DUMP = args.dump

    if not os.path.exists(DUMP):
        sys.exit("missing %s: build it with `fe build --backend native tools/syntax-dump --out-dir target/fe`" % DUMP)
    scratch = tempfile.mkdtemp(prefix="parser_vs_fe_")
    WORK = Workdir(scratch)
    failures = 0
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs)

    chars = detect_chars()
    print("fe check columns count %s" % ("characters" if chars else "bytes"))

    # 1. Real files.
    files = real_files()
    bad = [(p, o, out) for p, o, out in pool.map(lambda p: (p, *run_dump(p)), files) if o is not None]
    print("1. acceptance: %d real files, %d rejected" % (len(files), len(bad)))
    for p, o, out in bad[:20]:
        failures += 1
        if o == "crash":
            print("   CRASH %s: %s" % (os.path.relpath(p, ROOT), out[:200]))
        else:
            print("   REJECT %s at %s" % (os.path.relpath(p, ROOT), pos_of(open(p, "rb").read(), o, chars)))

    # 2. Seeds.
    seeds = seed_files()

    def judge_seed(item):
        name, src = item
        off, _ = parse_mutant(src)
        errs, problem = fe_check(src)
        return name, src, off, errs, problem

    seed_bad = 0
    seed_skipped = []
    for name, src, off, errs, problem in pool.map(judge_seed, seeds):
        if off is not None and off != "crash" and errs and pos_of(src, off, chars) == (errs[0][0], errs[0][1]):
            # Not declaration-only valid after all: both reject it in the same place, so it is
            # not a usable seed (the corpus keeps such files for the generator's own errors).
            seed_skipped.append(name)
            continue
        problems = [problem] if problem else []
        if off is not None:
            problems.append("parser rejects at %s" % (pos_of(src, off, chars) if off != "crash" else "crash",))
        if errs:
            problems.append("fe rejects at %s: %s" % ((errs[0][0], errs[0][1]), errs[0][3]))
        if problems:
            seed_bad += 1
            failures += 1
            print("   SEED %s: %s" % (name, "; ".join(problems)))
    print("2. seeds: %d seed files, %d disagreeing, %d rejected by both at the same place (not used as seeds: %s)"
          % (len(seeds), seed_bad, len(seed_skipped), ", ".join(seed_skipped) or "none"))
    seeds = [x for x in seeds if x[0] not in seed_skipped]

    # 3. Mutants.
    rng = random.Random(args.seed)
    prepared = []
    for name, src in seeds:
        toks = lex(src)
        prepared.append((name, src, toks, mutable_mask(src, toks)))
    jobs, seen, attempts = [], set(), 0
    while len(jobs) < args.mutants and attempts < args.mutants * 50:
        attempts += 1
        name, src, toks, mask = rng.choice(prepared)
        kind = rng.choice(KINDS)
        r = mutate(rng, src, toks, mask, kind)
        if r is None:
            continue
        m, desc, pos = r
        if m == src or (name, m) in seen:
            continue
        seen.add((name, m))
        jobs.append({"seed": name, "kind": kind, "src": m, "orig": src, "desc": desc, "pos": pos})

    def judge(job):
        job.update(verdict(job["src"], chars))
        return job

    results = list(pool.map(judge, jobs))
    pool.shutdown()

    by_class = collections.Counter(r["class"] for r in results)
    by_kind = collections.defaultdict(collections.Counter)
    for r in results:
        by_kind[r["kind"]][r["class"]] += 1
    classes = CLASSES + [c for c in EXTRA if by_class.get(c)]
    print("\n3. mutants: %d (seed %d)\n" % (len(results), args.seed))
    width = max(len(c) for c in classes)
    heads = ["delete", "dup", "swap", "insert", "del-nl", "nl->sp", "ins-nl"]
    print("%-*s  %s    total" % (width, "class", "  ".join("%7s" % h for h in heads)))
    for c in classes:
        print("%-*s  %s  %7d" % (width, c, "  ".join("%7d" % by_kind[k][c] for k in KINDS), by_class[c]))
    print("%-*s  %s  %7d" % (width, "total", "  ".join("%7d" % sum(by_kind[k].values()) for k in KINDS), len(results)))
    print("(columns: %s)" % ", ".join(KINDS))

    multi = [r for r in results if len(r["fe_errs"]) >= 2]
    not_lowest = [r for r in multi
                  if (r["fe_errs"][0][0], r["fe_errs"][0][1]) != min((e[0], e[1]) for e in r["fe_errs"])]
    print("\nfe check printed >= 2 parse errors for %d mutants; its first was not the lowest in %d"
          % (len(multi), len(not_lowest)))
    for r in not_lowest[:3]:
        print("   e.g. %s: %s" % (r["desc"], [(e[0], e[1]) for e in r["fe_errs"]]))

    for c in CLASSES + EXTRA:
        rs = [r for r in results if r["class"] == c]
        if not rs or c in ("agree-accept", "agree-reject-same-location"):
            continue
        label = "EXCUSED (parser skips these by design)" if c == "excused-skipped-construct" else "DISAGREEMENT"
        print("\n%s %s: %d (first %d)" % (label, c, len(rs), min(10, len(rs))))
        for r in rs[:10]:
            print(show_example(r))
            if c in EXTRA:
                print("    " + r["note"].strip()[:200])
    failures += sum(by_class[c] for c in FAILING)

    sigs = collections.OrderedDict()
    for r in results:
        if r["class"] in FAILING:
            sigs.setdefault((r["class"], r["sig"]), []).append(r)
    if sigs:
        print("\nDisagreements by signature (class | fe message | relation):")
        shown_sigs = 0
        for (c, sig), rs in sorted(sigs.items(), key=lambda kv: -len(kv[1])):
            print("  %4d  %s | %s" % (len(rs), c, sig))
            if args.reduce and shown_sigs < 15:
                shown_sigs += 1
                small = reduce_input(rs[0]["src"], rs[0]["class"], rs[0]["sig"], chars)
                print("        minimal input (%d bytes): %r" % (len(small), small.decode("utf8", "replace")))
                v = verdict(small, chars)
                print("        parser: %s    fe check: %s" % (v["ppos"] or "parses", v["fpos"] or "parses"))

    if args.keep:
        keep = os.path.join(scratch, "failing")
        os.makedirs(keep, exist_ok=True)
        for i, r in enumerate(x for x in results if x["class"] in FAILING):
            with open(os.path.join(keep, "%03d_%s.fe" % (i, r["class"])), "wb") as f:
                f.write(r["src"])
        print("\nkept scratch directory: %s" % scratch)
    else:
        shutil.rmtree(scratch, ignore_errors=True)
    print("\n%d failure(s), %.1fs" % (failures, time.time() - t0))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
