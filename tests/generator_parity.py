#!/usr/bin/env python3
"""Parity harness for the fe-codec-gen generator.

Runs the Rust generator on every case in tests/generator_corpus/ and, with --fe, the Fe
rewrite as well, then compares the two. Run from the repository root:

    python3 tests/generator_parity.py --rust target/debug/fe-codec-gen
    python3 tests/generator_parity.py --rust target/debug/fe-codec-gen --fe target/debug/fe-codec-gen

Each case is a directory holding src/*.fe and a case.toml:

    sources = ["src/models.fe"]        # --source arguments, in order
    modes = ["serializers", "eip712"]  # default: both
    syntax_error = true                # compare only the status and the path:line:col prefix
    expect_error = "text"              # the Rust run must fail with this text on stderr
    flows = ["out", "check", ...]      # see FLOWS

The Rust run is also checked against its expectation: a syntax_error case must fail with a
path:line:col: error: prefix, an expect_error case must fail with that text, and any other
case must succeed. Flows have no expectation of their own; their results are printed and,
with --fe, compared.

Every run starts from a fresh copy of the case in a temporary directory with a generated
fe.toml, and uses relative source paths so that messages do not contain temporary paths.
"""

import argparse
import difflib
import re
import shutil
import subprocess
import tempfile
import textwrap
import tomllib
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "generator_corpus"
OUTPUT = "src/serializers.fe"
HANDWRITTEN = b"// Handwritten\n"
FE_TOML = '[ingot]\nname = "test_models"\n'
MODE_FLAGS = {"serializers": [], "eip712": ["--eip712"]}
# out: --out. check: out, then --check. stale: out, apply the case's stale_edit (replace the
# first occurrence of its first string with its second in the first source), then --check. handwritten: a handwritten OUTPUT exists, then --out. missing: --check with
# no OUTPUT.
FLOWS = ("out", "check", "stale", "handwritten", "missing")
CASE_KEYS = {"sources", "modes", "syntax_error", "flows", "expect_error", "stale_edit"}
PREFIX = re.compile(r"^(.*?:\d+:\d+: error: )")


@dataclass
class Case:
    name: str
    path: Path
    sources: list
    modes: list
    syntax_error: bool
    flows: list
    expect_error: str | None
    stale_edit: list | None


@dataclass
class Step:
    label: str
    status: int
    stdout: bytes
    stderr: str


@dataclass
class Trace:
    case: Case
    mode: str
    flow: str | None
    steps: list
    written: bytes | None

    @property
    def name(self) -> str:
        return f"{self.case.name} [{self.mode}] {self.flow or 'generate'}"


def load_cases(only: str | None) -> list:
    cases = []
    for path in sorted(CORPUS.iterdir()):
        spec_path = path / "case.toml"
        if not spec_path.is_file() or (only and path.name != only):
            continue
        spec = tomllib.loads(spec_path.read_text(encoding="utf-8"))
        unknown = sorted(set(spec) - CASE_KEYS)
        if unknown:
            raise SystemExit(f"{spec_path}: unknown keys {unknown}")
        sources = spec["sources"]
        modes = spec.get("modes", list(MODE_FLAGS))
        flows = spec.get("flows", [])
        for source in sources:
            if not (path / source).is_file():
                raise SystemExit(f"{spec_path}: missing source {source}")
        for mode in modes:
            if mode not in MODE_FLAGS:
                raise SystemExit(f"{spec_path}: unknown mode {mode!r}")
        for flow in flows:
            if flow not in FLOWS:
                raise SystemExit(f"{spec_path}: unknown flow {flow!r}")
        stale_edit = spec.get("stale_edit")
        if "stale" in flows:
            if not (isinstance(stale_edit, list) and len(stale_edit) == 2):
                raise SystemExit(f"{spec_path}: the stale flow needs stale_edit = [old, new]")
            if stale_edit[0] not in (path / sources[0]).read_text(encoding="utf-8"):
                raise SystemExit(f"{spec_path}: stale_edit text not found in {sources[0]}")
        cases.append(
            Case(
                name=path.name,
                path=path,
                sources=sources,
                modes=modes,
                syntax_error=spec.get("syntax_error", False),
                flows=flows,
                expect_error=spec.get("expect_error"),
                stale_edit=stale_edit,
            )
        )
    if only and not cases:
        raise SystemExit(f"no case named {only!r} in {CORPUS}")
    return cases


def run_generator(binary: Path, cwd: Path, args: list) -> tuple:
    proc = subprocess.run(
        [str(binary), *args], cwd=cwd, capture_output=True, check=False
    )
    return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", errors="replace")


def execute(binary: Path, case: Case, mode: str, flow: str | None, ws: Path) -> tuple:
    sources = [arg for source in case.sources for arg in ("--source", source)]
    flags = MODE_FLAGS[mode]
    steps = []

    def run(label: str, extra: list) -> None:
        status, stdout, stderr = run_generator(binary, ws, [*sources, *flags, *extra])
        steps.append(Step(label, status, stdout, stderr))

    out = ["--out", OUTPUT]
    check = [*out, "--check"]
    if flow is None:
        run("generate", ["--stdout"])
    elif flow == "out":
        run("out", out)
    elif flow == "check":
        run("out", out)
        run("check", check)
    elif flow == "stale":
        run("out", out)
        source = ws / case.sources[0]
        old, new = case.stale_edit
        source.write_text(source.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
        run("check", check)
    elif flow == "handwritten":
        (ws / OUTPUT).write_bytes(HANDWRITTEN)
        run("out", out)
    elif flow == "missing":
        run("check", check)

    target = ws / OUTPUT
    written = target.read_bytes() if target.is_file() else None
    return steps, written


def run_trace(binary: Path, case: Case, mode: str, flow: str | None, keep: bool, kept: list) -> Trace:
    ws = Path(tempfile.mkdtemp(prefix=f"parity-{case.name}-"))
    try:
        shutil.copytree(case.path / "src", ws / "src")
        (ws / "fe.toml").write_text(FE_TOML, encoding="utf-8")
        steps, written = execute(binary, case, mode, flow, ws)
    finally:
        if keep:
            kept.append(ws)
        else:
            shutil.rmtree(ws, ignore_errors=True)
    return Trace(case, mode, flow, steps, written)


def first_line(text: str) -> str:
    return text.splitlines()[0] if text else "<empty stderr>"


def prefix_of(text: str) -> str | None:
    match = PREFIX.match(first_line(text))
    return match.group(1) if match else None


def describe(trace: Trace) -> str:
    line = f"{trace.name}: " + ", ".join(f"{s.label} exit {s.status}" for s in trace.steps)
    if trace.written is not None:
        line += f", output file {len(trace.written)} bytes"
    failing = [s for s in trace.steps if s.status != 0]
    if failing:
        line += f"\n    {failing[0].label}: {first_line(failing[0].stderr)}"
    return line


def expectation_problem(trace: Trace) -> str | None:
    if trace.flow is not None:
        return None
    case = trace.case
    step = trace.steps[0]
    line = first_line(step.stderr)
    if case.syntax_error:
        if step.status == 0:
            return "expected a syntax error, generator succeeded"
        if prefix_of(step.stderr) is None:
            return f"first stderr line lacks the path:line:col: error: prefix: {line}"
        return None
    if case.expect_error is not None:
        if step.status == 0:
            return f"expected error {case.expect_error!r}, generator succeeded"
        if case.expect_error not in step.stderr:
            return f"stderr lacks {case.expect_error!r}: {line}"
        return None
    if step.status != 0:
        return f"expected success, generator exited {step.status}: {line}"
    return None


def diff_bytes(rust: bytes | None, fe: bytes | None) -> str | None:
    if rust == fe:
        return None
    if rust is None or fe is None:
        def show(value):
            return "no file" if value is None else f"{len(value)} bytes"
        return f"rust: {show(rust)}\nfe:   {show(fe)}"
    rust_lines = rust.decode("utf-8", errors="replace").splitlines(keepends=True)
    fe_lines = fe.decode("utf-8", errors="replace").splitlines(keepends=True)
    diff = "".join(difflib.unified_diff(rust_lines, fe_lines, "rust", "fe"))
    return diff or f"bytes differ: rust {len(rust)}, fe {len(fe)}"


def compare_step(rust: Step, fe: Step, syntax: bool) -> str | None:
    if rust.status != fe.status:
        return (
            f"exit status: rust {rust.status}, fe {fe.status}\n"
            f"rust stderr: {first_line(rust.stderr)}\n"
            f"fe stderr:   {first_line(fe.stderr)}"
        )
    if rust.status == 0:
        return diff_bytes(rust.stdout, fe.stdout)
    if syntax:
        rust_prefix, fe_prefix = prefix_of(rust.stderr), prefix_of(fe.stderr)
        if rust_prefix is None or rust_prefix != fe_prefix:
            return f"syntax error prefix: rust {rust_prefix!r}, fe {fe_prefix!r}"
        return None
    return diff_bytes(rust.stderr.encode(), fe.stderr.encode())


def compare(rust: Trace, fe: Trace) -> list:
    results = []
    for r, f in zip(rust.steps, fe.steps):
        problem = compare_step(r, f, rust.case.syntax_error)
        results.append((f"{rust.name} :: {r.label}", problem))
    if rust.flow is not None:
        results.append((f"{rust.name} :: output file", diff_bytes(rust.written, fe.written)))
    return results


def binary_path(value: Path, option: str) -> Path:
    path = value.resolve()
    if not path.is_file():
        raise SystemExit(f"{option}: no such file: {value}")
    return path


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run fe-codec-gen (Rust) on the generator corpus, optionally comparing the Fe rewrite."
    )
    parser.add_argument("--rust", required=True, type=Path, help="Rust fe-codec-gen binary")
    parser.add_argument("--fe", type=Path, help="Fe fe_codec_gen binary to compare with the Rust one")
    parser.add_argument("--case", help="run only the case with this directory name")
    parser.add_argument("--mode", choices=sorted(MODE_FLAGS), help="run only this mode")
    parser.add_argument("--keep", action="store_true", help="keep temporary directories and print their paths")
    args = parser.parse_args(argv)

    rust = binary_path(args.rust, "--rust")
    fe = binary_path(args.fe, "--fe") if args.fe else None
    cases = load_cases(args.case)
    kept: list = []
    passed = failed = runs = 0

    for case in cases:
        for mode in case.modes:
            if args.mode and mode != args.mode:
                continue
            for flow in [None, *case.flows]:
                rust_trace = run_trace(rust, case, mode, flow, args.keep, kept)
                runs += 1
                print(describe(rust_trace))
                results = []
                if flow is None:
                    results.append((f"{rust_trace.name} :: expectation", expectation_problem(rust_trace)))
                if fe is not None:
                    fe_trace = run_trace(fe, case, mode, flow, args.keep, kept)
                    results.extend(compare(rust_trace, fe_trace))
                for name, problem in results:
                    if problem is None:
                        passed += 1
                        print(f"  PASS {name}")
                    else:
                        failed += 1
                        print(f"  FAIL {name}")
                        print(textwrap.indent(problem.rstrip("\n"), "      "))

    print(f"\ncases: {len(cases)}, runs: {runs}, checks: {passed + failed}, passed: {passed}, failed: {failed}")
    if kept:
        print("kept temporary directories:")
        for ws in kept:
            print(f"  {ws}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
