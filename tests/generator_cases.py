#!/usr/bin/env python3
"""Specification tests for the command line and diagnostics of fe_codec_gen.

Runs every case in tests/generator_corpus/ against the Fe generator (tools/fe-codec-gen) and
checks its expectations, with no reference generator and no stored output. Run from anywhere:

    python3 tests/generator_cases.py [--case NAME] [--keep]

Each corpus case is a directory holding src/*.fe and a case.toml:

    sources = ["src/models.fe"]        # --source arguments, in order
    modes = ["serializers", "eip712"]  # default: both
    expect_stderr = "src/models.fe:3:5: error: message\\n"
                                       # semantic error: exit 1 and exactly this stderr
    syntax_error = true                # exit 1, stderr starts with src/x.fe:L:C: error: where
                                       # L:C is what `fe check` reports for the first parse error
    compile = false                    # success cases are compiled with `fe check`; skip it
    compile_note = "why"               #   (a reason is required)
    compile_files = {"src/m.fe" = "repo/relative/path.fe"}
                                       # extra modules for the compile check only
    known_defect = "what is wrong"     # the expectation is right but the generator disagrees:
                                       # failures print as XFAIL and do not fail the run; a case
                                       # that passes anyway fails (remove the key)
    flows = ["out", "check", ...]      # see FLOWS
    stale_edit = ["old", "new"]        # the stale flow: edit applied to the first source
    break_edit = ["old", "new"]        # the failed_run_keeps_output flow: a semantic break of
    break_stderr = "...\\n"            #   the first source, and the exact stderr it produces

A case without expect_stderr and syntax_error is a success: in every mode the generator exits 0
with an empty stderr, prints the same bytes twice, and the output compiles in an ingot that
depends on fe_codec_core and fe_codec_eip712.

The expected line:col of a semantic error is part of the specification and is written by hand
from the anchoring rule of the check: item errors anchor at the item's first attribute or doc
comment, otherwise its first modifier or keyword; field and variant errors the same within the
field or variant; type errors at the first token of the field type; "duplicate declaration" at
the name token; where-clause errors at `where`; generic-parameter errors at the parameter's
first token; attribute errors at their `#`; "no #[serializable] struct or enum found" and "no
#[eip712] struct found" at 1:1 of the first source; EIP-712 field errors at the field, prefixed
"field 'NAME': ". Lines and columns are 1-based, columns count characters (UTF-8 code points, as fe check does), and only \\n ends a line.

Every run starts from a fresh temporary directory with relative source paths, so messages do
not contain temporary paths. Besides the corpus there is a table of command line cases
(build_cli_cases below), and a coverage check: every message in tools/codegen/src/schema.fe and eip712.fe must
be expected by a passing case, or be listed in UNREACHABLE with a reason.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CORPUS = HERE / "generator_corpus"
GENERATOR = ROOT / "tools" / "fe-codec-gen"
MESSAGE_SOURCES = [ROOT / "tools/codegen/src/schema.fe", ROOT / "tools/codegen/src/eip712.fe"]

OUTPUT = "src/serializers.fe"
HANDWRITTEN = b"// Handwritten\n"
FE_TOML = '[ingot]\nname = "test_models"\n'
MODE_FLAGS = {"serializers": [], "eip712": ["--eip712"]}
STALE = f"{OUTPUT}: error: generated serializers are missing or stale\n"
HANDWRITTEN_ERROR = f"{OUTPUT}: error: refusing to replace handwritten source\n"
NOT_REGULAR = f"{OUTPUT}: error: expected a regular output file\n"

# out: --out writes the module the generator prints with --stdout. check: out, then --check
# succeeds. stale: out, edit the source with stale_edit, then --check reports a stale file and
# leaves it alone. handwritten: --out and --check refuse a handwritten OUTPUT and leave it
# alone. missing: --check with no OUTPUT fails and creates nothing. stdout_writes_nothing:
# --stdout creates no file. idempotent: --out twice leaves the bytes unchanged. failed_run_
# keeps_output: after a good --out, a semantically broken source fails --out and leaves the
# output byte for byte. symlink_output: OUTPUT is a symlink to a regular file, and is refused.
# directory_output: OUTPUT is a directory, and is refused.
FLOWS = (
    "out",
    "check",
    "stale",
    "handwritten",
    "missing",
    "stdout_writes_nothing",
    "idempotent",
    "failed_run_keeps_output",
    "symlink_output",
    "directory_output",
)
# On a case that fails to generate, every run fails with the case's diagnostic first.
FAILING_FLOWS = {
    "out",
    "handwritten",
    "missing",
    "stdout_writes_nothing",
    "symlink_output",
    "directory_output",
}
CASE_KEYS = {
    "sources",
    "modes",
    "syntax_error",
    "expect_stderr",
    "flows",
    "stale_edit",
    "break_edit",
    "break_stderr",
    "compile",
    "compile_note",
    "compile_files",
    "known_defect",
}
DIAGNOSTIC = re.compile(r"^(?P<path>[^:\n]+):(?P<line>\d+):(?P<col>\d+): error: (?P<message>[^\n]+)\n$")

# Messages in the Fe sources that no input can produce, each with the reason. The parser
# rejects these inputs first, or the code is unreachable by construction.
UNREACHABLE = {
    "missing name": "a struct, enum, field or variant without a name is a syntax error, which the parser reports first",
    "missing generic parameter name": "a generic parameter without a name is a syntax error (`expected a generic parameter`)",
    "missing const parameter type": "a const parameter without a type is a syntax error (`expected `:` and the const parameter type`)",
    "#[eip712] applies to structs": "only structs are ever marked #[eip712]; on an enum the attribute is ignored and no root is found",
}


def find_fe() -> dict:
    env = dict(os.environ)
    if shutil.which("fe", path=env.get("PATH")) is None:
        env["PATH"] = f"{Path.home() / '.fe' / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    if shutil.which("fe", path=env["PATH"]) is None:
        raise SystemExit("the fe toolchain is not on PATH (export PATH=$HOME/.fe/bin:$PATH)")
    return env


ENV = find_fe()


# ---------------------------------------------------------------------------------------------
# Running things


@dataclass
class Step:
    status: int
    stdout: bytes
    stderr: str


def run_generator(cwd: Path, args: list, close_stdout: bool = False) -> Step:
    command = [str(GENERATOR), *args]
    if close_stdout:
        command = ["sh", "-c", 'exec "$0" "$@" >&-', *command]
    proc = subprocess.run(command, cwd=cwd, capture_output=True, env=ENV, check=False)
    return Step(proc.returncode, proc.stdout, proc.stderr.decode("utf-8", errors="replace"))


def run_fe_check(directory: Path) -> tuple:
    proc = subprocess.run(
        ["fe", "check", str(directory)], cwd=directory, capture_output=True, env=ENV, check=False
    )
    return proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", errors="replace")


class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.xfailed = 0
        self.failed_cases: set = set()
        self.known: dict = {}  # case name -> known_defect text
        self.problems: dict = {}  # case name -> number of failing checks

    def check(self, case: str, name: str, problem) -> bool:
        if problem is None:
            self.passed += 1
            print(f"  PASS {name}")
            return True
        self.problems[case] = self.problems.get(case, 0) + 1
        if case in self.known:
            self.xfailed += 1
            print(f"  XFAIL {name}")
            for line in str(problem).rstrip("\n").splitlines():
                print(f"      {line}")
            return False
        self.failed += 1
        self.failed_cases.add(case)
        print(f"  FAIL {name}")
        for line in str(problem).rstrip("\n").splitlines():
            print(f"      {line}")
        return False


def show(text: str) -> str:
    return repr(text)


def same_text(actual: str, expected: str, what: str = "stderr") -> str | None:
    if actual == expected:
        return None
    return f"{what}:\n  expected {show(expected)}\n  actual   {show(actual)}"


# ---------------------------------------------------------------------------------------------
# The corpus


@dataclass
class Case:
    name: str
    path: Path
    sources: list
    modes: list
    syntax_error: bool
    expect_stderr: str | None
    flows: list
    stale_edit: list | None
    break_edit: list | None
    break_stderr: str | None
    compile: bool
    compile_files: dict
    known_defect: str | None

    @property
    def kind(self) -> str:
        if self.syntax_error:
            return "syntax"
        return "semantic" if self.expect_stderr is not None else "success"


def load_cases(only: str | None) -> list:
    cases = []
    for path in sorted(CORPUS.iterdir()):
        spec_path = path / "case.toml"
        if not spec_path.is_file():
            continue
        spec = tomllib.loads(spec_path.read_text(encoding="utf-8"))
        where = str(spec_path.relative_to(ROOT))
        unknown = sorted(set(spec) - CASE_KEYS)
        if unknown:
            raise SystemExit(f"{where}: unknown keys {unknown}")
        sources = spec["sources"]
        modes = spec.get("modes", list(MODE_FLAGS))
        flows = spec.get("flows", [])
        syntax_error = spec.get("syntax_error", False)
        expect = spec.get("expect_stderr")
        for source in sources:
            if not (path / source).is_file():
                raise SystemExit(f"{where}: missing source {source}")
        for mode in modes:
            if mode not in MODE_FLAGS:
                raise SystemExit(f"{where}: unknown mode {mode!r}")
        for flow in flows:
            if flow not in FLOWS:
                raise SystemExit(f"{where}: unknown flow {flow!r}")
        if syntax_error and expect is not None:
            raise SystemExit(f"{where}: syntax_error and expect_stderr are exclusive")
        if expect is not None and not DIAGNOSTIC.match(expect):
            raise SystemExit(f"{where}: expect_stderr must be one `path:line:col: error: message` line ending in \\n")
        failing = syntax_error or expect is not None
        for flow in flows:
            if failing and flow not in FAILING_FLOWS:
                raise SystemExit(f"{where}: flow {flow!r} needs a case that generates")
        first = (path / sources[0]).read_text(encoding="utf-8")
        for key, flow in (("stale_edit", "stale"), ("break_edit", "failed_run_keeps_output")):
            edit = spec.get(key)
            if flow in flows:
                if not (isinstance(edit, list) and len(edit) == 2 and all(isinstance(e, str) for e in edit)):
                    raise SystemExit(f"{where}: the {flow} flow needs {key} = [old, new]")
                if edit[0] not in first:
                    raise SystemExit(f"{where}: {key} text not found in {sources[0]}")
        if "failed_run_keeps_output" in flows:
            if not (isinstance(spec.get("break_stderr"), str) and DIAGNOSTIC.match(spec["break_stderr"])):
                raise SystemExit(f"{where}: the failed_run_keeps_output flow needs break_stderr")
        compile_ = spec.get("compile", True)
        if not compile_ and not spec.get("compile_note"):
            raise SystemExit(f"{where}: compile = false needs a compile_note")
        if only and path.name != only:
            continue
        cases.append(
            Case(
                name=path.name,
                path=path,
                sources=sources,
                modes=modes,
                syntax_error=syntax_error,
                expect_stderr=expect,
                flows=flows,
                stale_edit=spec.get("stale_edit"),
                break_edit=spec.get("break_edit"),
                break_stderr=spec.get("break_stderr"),
                compile=compile_,
                compile_files=spec.get("compile_files", {}),
                known_defect=spec.get("known_defect"),
            )
        )
    return cases


def make_workspace(case: Case, kept: list, keep: bool) -> Path:
    ws = Path(tempfile.mkdtemp(prefix=f"cases-{case.name}-"))
    shutil.copytree(case.path / "src", ws / "src")
    (ws / "fe.toml").write_text(FE_TOML, encoding="utf-8")
    if keep:
        kept.append(ws)
    return ws


def source_args(case: Case) -> list:
    return [arg for source in case.sources for arg in ("--source", source)]


# ---------------------------------------------------------------------------------------------
# What the official compiler says about a malformed source

_syntax_cache: dict = {}


def official_syntax_error(text: bytes) -> tuple | None:
    """The (line, column) of the first parse error `fe check` reports for this module."""
    if text in _syntax_cache:
        return _syntax_cache[text]
    directory = Path(tempfile.mkdtemp(prefix="cases-syntax-"))
    try:
        (directory / "src").mkdir()
        (directory / "fe.toml").write_text('[ingot]\nname = "syntax_probe"\nversion = "0.1.0"\n', encoding="utf-8")
        (directory / "src" / "lib.fe").write_bytes(text)
        _, output = run_fe_check(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    found = re.search(r"error\[1-\d+\][^\n]*\n\s*┌─ \S*src/lib\.fe:(\d+):(\d+)", output)
    result = (int(found.group(1)), int(found.group(2))) if found else None
    _syntax_cache[text] = result
    return result


def expected_syntax_prefix(case: Case) -> tuple:
    """(prefix, problem): where the generator must report the first malformed source."""
    for source in case.sources:
        location = official_syntax_error((case.path / source).read_bytes())
        if location is not None:
            return f"{source}:{location[0]}:{location[1]}: error: ", None
    return None, "`fe check` reports no parse error (error[1-...]) in any source"


# ---------------------------------------------------------------------------------------------
# Compiling generated modules


def compile_problem(case: Case, mode: str, modules: dict) -> str | None:
    directory = Path(tempfile.mkdtemp(prefix=f"cases-compile-{case.name}-"))
    try:
        shutil.copytree(case.path / "src", directory / "src")
        if not (directory / "src" / "lib.fe").exists():
            (directory / "src" / "lib.fe").write_text("\n", encoding="utf-8")
        for name, source in case.compile_files.items():
            (directory / name).write_bytes((ROOT / source).read_bytes())
        for name, content in modules.items():
            (directory / "src" / name).write_bytes(content)
        relative = os.path.relpath(ROOT, os.path.realpath(directory))
        (directory / "fe.toml").write_text(
            '[ingot]\nname = "generated_case"\nversion = "0.1.0"\n\n[dependencies]\n'
            f'fe_codec_core = "{relative}/core"\n'
            f'fe_codec_eip712 = "{relative}/formats/eip712"\n',
            encoding="utf-8",
        )
        status, output = run_fe_check(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    if status != 0 or re.search(r"^error", output, re.MULTILINE):
        return f"fe check exit {status}:\n" + "\n".join(output.splitlines()[:30])
    return None


# ---------------------------------------------------------------------------------------------
# Flows


def snapshot(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            result[relative] = ("link", os.readlink(path))
        elif path.is_dir():
            result[relative] = ("dir",)
        else:
            result[relative] = ("file", path.read_bytes())
    return result


def diff_snapshots(before: dict, after: dict) -> str | None:
    if before == after:
        return None
    lines = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            def brief(value):
                if value is None:
                    return "absent"
                return value[0] if value[0] != "file" else f"file of {len(value[1])} bytes"
            lines.append(f"{key}: {brief(before.get(key))} -> {brief(after.get(key))}")
    return "the workspace changed:\n" + "\n".join(lines)


class FlowContext:
    def __init__(self, case: Case, mode: str, ws: Path, report: Report, reference: Step) -> None:
        self.case = case
        self.mode = mode
        self.ws = ws
        self.report = report
        self.reference = reference  # the plain --stdout run, in a fresh workspace
        self.flow = ""
        self.failure = None  # (exact stderr or None, prefix or None) when the case fails

    def run(self, extra: list) -> Step:
        return run_generator(self.ws, [*source_args(self.case), *MODE_FLAGS[self.mode], *extra])

    def check(self, label: str, problem) -> None:
        self.report.check(self.case.name, f"{self.case.name} [{self.mode}] {self.flow}: {label}", problem)

    def verdict(self, step: Step, status: int, stderr: str, stdout: bytes | None = b"") -> str | None:
        """Checks a run: its own expectation on a case that generates, the diagnostic otherwise."""
        problems = []
        if self.failure is not None:
            exact, prefix = self.failure
            status, stdout = 1, b""
            if exact is not None:
                problems.append(same_text(step.stderr, exact))
            elif not step.stderr.startswith(prefix):
                problems.append(f"stderr should start with {show(prefix)}, got {show(step.stderr)}")
        else:
            problems.append(same_text(step.stderr, stderr))
        if step.status != status:
            problems.append(f"exit status: expected {status}, got {step.status}")
        if stdout is not None and step.stdout != stdout:
            problems.append(f"stdout: expected {len(stdout)} bytes, got {len(step.stdout)}")
        problems = [p for p in problems if p]
        return "\n".join(problems) if problems else None

    def file_is(self, relative: str, expected: bytes | None) -> str | None:
        path = self.ws / relative
        if expected is None:
            return f"{relative} exists" if os.path.lexists(path) else None
        if not path.is_file():
            return f"{relative} is not a regular file"
        actual = path.read_bytes()
        if actual != expected:
            return f"{relative} differs: expected {len(expected)} bytes, got {len(actual)}"
        return None

    @property
    def generated(self) -> bytes | None:
        return None if self.failure is not None else self.reference.stdout


def flow_out(c: FlowContext) -> None:
    c.check("--out", c.verdict(c.run(["--out", OUTPUT]), 0, ""))
    c.check("output file is what --stdout prints", c.file_is(OUTPUT, c.generated))


def flow_check(c: FlowContext) -> None:
    c.check("--out", c.verdict(c.run(["--out", OUTPUT]), 0, ""))
    c.check("--check on a current file", c.verdict(c.run(["--out", OUTPUT, "--check"]), 0, ""))
    c.check("output file unchanged", c.file_is(OUTPUT, c.generated))


def flow_stale(c: FlowContext) -> None:
    c.check("--out", c.verdict(c.run(["--out", OUTPUT]), 0, ""))
    source = c.ws / c.case.sources[0]
    old, new = c.case.stale_edit
    source.write_text(source.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
    edited = c.run(["--stdout"])
    problem = c.verdict(edited, 0, "", stdout=None)
    if problem is None and edited.stdout == c.generated:
        problem = "stale_edit does not change the generated module"
    c.check("the edited source generates a different module", problem)
    c.check("--check on a stale file", c.verdict(c.run(["--out", OUTPUT, "--check"]), 1, STALE))
    c.check("stale file untouched", c.file_is(OUTPUT, c.generated))


def flow_handwritten(c: FlowContext) -> None:
    (c.ws / OUTPUT).write_bytes(HANDWRITTEN)
    c.check("--out refuses to replace it", c.verdict(c.run(["--out", OUTPUT]), 1, HANDWRITTEN_ERROR))
    c.check("handwritten file untouched", c.file_is(OUTPUT, HANDWRITTEN))
    c.check("--check calls it stale", c.verdict(c.run(["--out", OUTPUT, "--check"]), 1, STALE))
    c.check("handwritten file untouched by --check", c.file_is(OUTPUT, HANDWRITTEN))


def flow_missing(c: FlowContext) -> None:
    c.check("--check with no output file", c.verdict(c.run(["--out", OUTPUT, "--check"]), 1, STALE))
    c.check("no file was created", c.file_is(OUTPUT, None))


def flow_stdout_writes_nothing(c: FlowContext) -> None:
    before = snapshot(c.ws)
    step = c.run(["--stdout"])
    c.check("--stdout", c.verdict(step, 0, "", stdout=c.generated))
    c.check("no file was created or changed", diff_snapshots(before, snapshot(c.ws)))
    c.check("no file in the working directory", None if not list(c.ws.glob("*.fe")) else "a .fe file appeared")


def flow_idempotent(c: FlowContext) -> None:
    c.check("first --out", c.verdict(c.run(["--out", OUTPUT]), 0, ""))
    first = (c.ws / OUTPUT).read_bytes() if (c.ws / OUTPUT).is_file() else None
    c.check("second --out succeeds", c.verdict(c.run(["--out", OUTPUT]), 0, ""))
    c.check("bytes unchanged by the second run", c.file_is(OUTPUT, first))
    c.check("bytes are the generated module", c.file_is(OUTPUT, c.generated))


def flow_failed_run_keeps_output(c: FlowContext) -> None:
    c.check("--out", c.verdict(c.run(["--out", OUTPUT]), 0, ""))
    source = c.ws / c.case.sources[0]
    old, new = c.case.break_edit
    source.write_text(source.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
    c.check("--out on the broken source", c.verdict(c.run(["--out", OUTPUT]), 1, c.case.break_stderr))
    c.check("output file unchanged byte for byte", c.file_is(OUTPUT, c.generated))


def flow_symlink_output(c: FlowContext) -> None:
    # The target holds exactly the module that would be generated, so a generator that followed
    # the link would find it current and succeed.
    target = c.ws / "target.fe.txt"
    content = c.generated if c.generated is not None else HANDWRITTEN
    target.write_bytes(content)
    os.symlink("../target.fe.txt", c.ws / OUTPUT)
    c.check("--out through a symlink", c.verdict(c.run(["--out", OUTPUT]), 1, NOT_REGULAR))
    c.check("--check through a symlink", c.verdict(c.run(["--out", OUTPUT, "--check"]), 1, NOT_REGULAR))
    c.check("target untouched", None if target.read_bytes() == content else "the symlink target changed")
    still = (c.ws / OUTPUT).is_symlink() and os.readlink(c.ws / OUTPUT) == "../target.fe.txt"
    c.check("the symlink is still there", None if still else "the symlink was replaced")


def flow_directory_output(c: FlowContext) -> None:
    (c.ws / OUTPUT).mkdir()
    c.check("--out onto a directory", c.verdict(c.run(["--out", OUTPUT]), 1, NOT_REGULAR))
    c.check("--check onto a directory", c.verdict(c.run(["--out", OUTPUT, "--check"]), 1, NOT_REGULAR))
    c.check("the directory is untouched", None if (c.ws / OUTPUT).is_dir() and not any((c.ws / OUTPUT).iterdir()) else "the directory changed")


FLOW_FUNCTIONS = {
    "out": flow_out,
    "check": flow_check,
    "stale": flow_stale,
    "handwritten": flow_handwritten,
    "missing": flow_missing,
    "stdout_writes_nothing": flow_stdout_writes_nothing,
    "idempotent": flow_idempotent,
    "failed_run_keeps_output": flow_failed_run_keeps_output,
    "symlink_output": flow_symlink_output,
    "directory_output": flow_directory_output,
}


# ---------------------------------------------------------------------------------------------
# Running a corpus case


def run_case(case: Case, report: Report, keep: bool, kept: list) -> None:
    print(f"{case.name} ({case.kind})")
    if case.known_defect:
        report.known[case.name] = case.known_defect
        run_case_checks(case, report, keep, kept)
        if not report.problems.get(case.name):
            report.check(case.name, f"{case.name}: known_defect is set but every check passes", "remove known_defect from case.toml")
        return
    run_case_checks(case, report, keep, kept)


def run_case_checks(case: Case, report: Report, keep: bool, kept: list) -> None:
    prefix = None
    if case.syntax_error:
        prefix, problem = expected_syntax_prefix(case)
        report.check(case.name, f"{case.name}: the official compiler reports a parse error", problem)
        if prefix is None:
            return
    for mode in case.modes:
        label = f"{case.name} [{mode}]"
        ws = make_workspace(case, kept, keep)
        try:
            generate = run_generator(ws, [*source_args(case), *MODE_FLAGS[mode], "--stdout"])
            reference = generate
            if case.kind == "success":
                problems = []
                if generate.status != 0:
                    problems.append(f"exit status {generate.status}: {generate.stderr.strip()}")
                if generate.stderr != "":
                    problems.append(f"stderr should be empty, got {show(generate.stderr)}")
                if not generate.stdout:
                    problems.append("stdout is empty")
                report.check(case.name, f"{label} generate", "\n".join(problems) or None)
                again = run_generator(ws, [*source_args(case), *MODE_FLAGS[mode], "--stdout"])
                report.check(
                    case.name,
                    f"{label} the same command twice gives identical output",
                    None if again.stdout == generate.stdout and again.status == 0 else "the two runs differ",
                )
                if case.compile and generate.status == 0:
                    modules = {}
                    if mode == "eip712":
                        plain = run_generator(ws, [*source_args(case), "--stdout"])
                        if plain.status == 0:
                            modules["serializers.fe"] = plain.stdout
                        modules["eip712.fe"] = generate.stdout
                    else:
                        modules["serializers.fe"] = generate.stdout
                    report.check(case.name, f"{label} generated module compiles", compile_problem(case, mode, modules))
            else:
                problems = []
                if generate.status != 1:
                    problems.append(f"exit status: expected 1, got {generate.status}")
                if case.kind == "semantic":
                    problems.append(same_text(generate.stderr, case.expect_stderr))
                elif not generate.stderr.startswith(prefix):
                    problems.append(f"stderr should start with {show(prefix)}, got {show(generate.stderr)}")
                if generate.stdout:
                    problems.append("stdout should be empty")
                report.check(case.name, f"{label} generate", "\n".join(p for p in problems if p) or None)
        finally:
            if not keep:
                shutil.rmtree(ws, ignore_errors=True)
        for flow in case.flows:
            ws = make_workspace(case, kept, keep)
            try:
                context = FlowContext(case, mode, ws, report, reference)
                context.flow = flow
                if case.kind == "semantic":
                    context.failure = (case.expect_stderr, None)
                elif case.kind == "syntax":
                    context.failure = (None, prefix)
                FLOW_FUNCTIONS[flow](context)
            finally:
                if not keep:
                    shutil.rmtree(ws, ignore_errors=True)


# ---------------------------------------------------------------------------------------------
# The command line table
#
# Each entry runs the generator in a fresh workspace holding fe.toml and src/models.fe (a valid
# model) plus its own files. {ws} stands for the workspace path in arguments and in the expected
# text. An entry states the exit status and the exact stderr (or a prefix and the substrings
# the text must contain), and stdout is empty unless given. A run that fails changes nothing in
# the workspace.

GOOD = "#[serializable]\npub struct Item { pub id: u8 }\n"
BASE_FILES = {"fe.toml": FE_TOML, "src/models.fe": GOOD}
MODULE_ERROR = "{path}: error: expected a named Fe module file\n"
TOP_LEVEL_ERROR = "{path}: error: expected a top-level module in an ingot's src directory\n"
OTHER_INGOT = {"other/fe.toml": FE_TOML, "other/src/models.fe": GOOD, "other/src/x.fe": GOOD}


@dataclass
class CliCase:
    name: str
    args: list
    status: int = 1
    stderr: str | None = None
    stderr_prefix: str | None = None
    stderr_contains: tuple = ()
    stdout: bytes | str | None = b""
    stdout_prefix: str | None = None
    stdout_contains: tuple = ()
    files: dict | None = None
    bare: bool = False  # no fe.toml and no src/models.fe
    setup: object = None
    same_as: list | None = None  # stdout must equal that of these arguments, run beforehand
    writes: dict | None = None  # relative path -> "generated": the file must equal the --stdout run
    changes: bool = False  # the run may change the workspace
    close_stdout: bool = False  # run with standard output closed


def cli(name: str, args: list, **kw) -> CliCase:
    return CliCase(name, args, **kw)


def build_cli_cases() -> list:
    cases = []
    stdout = ["--stdout"]

    def bad_module(label: str, filename: str) -> None:
        path = f"src/{filename}"
        cases.append(
            cli(
                f"module_name_{label}",
                ["--source", path, *stdout],
                files={path: GOOD},
                stderr=MODULE_ERROR.format(path=path),
            )
        )

    for label, filename in [
        ("lib", "lib.fe"),
        ("underscore", "_.fe"),
        ("fn_keyword", "fn.fe"),
        ("struct_keyword", "struct.fe"),
        ("enum_keyword", "enum.fe"),
        ("use_keyword", "use.fe"),
        ("pub_keyword", "pub.fe"),
        ("contract_keyword", "contract.fe"),
        ("dash", "a-b.fe"),
        ("leading_digit", "1a.fe"),
        ("txt_extension", "x.txt"),
        ("no_extension", "models"),
        ("double_extension", "models.fe.txt"),
        ("dotted_stem", "a.b.fe"),
        ("only_extension", ".fe"),
        ("non_ascii", "ünï.fe"),
    ]:
        bad_module(label, filename)
    for label, filename in [("lib", "lib.fe"), ("underscore", "_.fe"), ("fn_keyword", "fn.fe"), ("dash", "a-b.fe"), ("txt", "x.txt")]:
        path = f"src/{filename}"
        cases.append(
            cli(
                f"module_name_out_{label}",
                ["--source", "src/models.fe", "--out", path],
                stderr=MODULE_ERROR.format(path=path),
            )
        )
    cases.append(
        cli(
            "module_name_source_error_comes_before_out_error",
            ["--source", "src/lib.fe", "--out", "src/x.txt"],
            files={"src/lib.fe": GOOD},
            stderr=MODULE_ERROR.format(path="src/lib.fe"),
        )
    )
    cases.append(
        cli(
            "module_name_absolute_path_is_printed_as_given",
            ["--source", "{ws}/src/lib.fe", *stdout],
            files={"src/lib.fe": GOOD},
            stderr="{ws}/src/lib.fe: error: expected a named Fe module file\n",
        )
    )
    for label, filename in [("capital", "Lib.fe"), ("leading_underscore", "_x.fe"), ("digits", "x1.fe"), ("lib_prefix", "library.fe")]:
        path = f"src/{filename}"
        cases.append(
            cli(
                f"module_name_accepted_{label}",
                ["--source", path, *stdout],
                status=0,
                stdout=None,
                files={path: GOOD},
            )
        )

    # Where the sources are.
    cases += [
        cli(
            "source_in_a_subdirectory_of_src",
            ["--source", "src/nested/x.fe", *stdout],
            files={"src/nested/x.fe": GOOD},
            stderr=TOP_LEVEL_ERROR.format(path="src/nested/x.fe"),
        ),
        cli(
            "source_at_the_ingot_root",
            ["--source", "models.fe", *stdout],
            files={"models.fe": GOOD},
            stderr=TOP_LEVEL_ERROR.format(path="models.fe"),
        ),
        cli(
            "source_in_src_without_a_manifest",
            ["--source", "lone/src/models.fe", *stdout],
            files={"lone/src/models.fe": GOOD},
            stderr=TOP_LEVEL_ERROR.format(path="lone/src/models.fe"),
        ),
        cli(
            "source_in_src_whose_manifest_is_a_directory",
            ["--source", "odd/src/models.fe", *stdout],
            files={"odd/src/models.fe": GOOD, "odd/fe.toml/keep": ""},
            stderr=TOP_LEVEL_ERROR.format(path="odd/src/models.fe"),
        ),
        cli(
            "out_in_a_directory_that_is_not_src",
            ["--source", "src/models.fe", "--out", "x.fe"],
            stderr=TOP_LEVEL_ERROR.format(path="x.fe"),
        ),
        cli(
            "out_in_a_subdirectory_of_src",
            ["--source", "src/models.fe", "--out", "src/nested/x.fe"],
            files={"src/nested/keep": ""},
            stderr=TOP_LEVEL_ERROR.format(path="src/nested/x.fe"),
        ),
        cli(
            "sources_from_two_ingots",
            ["--source", "src/models.fe", "--source", "other/src/x.fe", *stdout],
            files=OTHER_INGOT,
            stderr="error: every --source must be in the same ingot's src directory\n",
        ),
        cli(
            "sources_from_two_ingots_same_module_name",
            ["--source", "src/models.fe", "--source", "other/src/models.fe", *stdout],
            files=OTHER_INGOT,
            stderr="error: every --source must be in the same ingot's src directory\n",
        ),
        cli(
            "same_source_twice",
            ["--source", "src/models.fe", "--source", "src/models.fe", *stdout],
            stderr="src/models.fe: error: source given twice\n",
        ),
        cli(
            "same_source_twice_second_spelling_is_printed",
            ["--source", "src/models.fe", "--source", "./src/models.fe", *stdout],
            stderr="./src/models.fe: error: source given twice\n",
        ),
        cli(
            "same_source_twice_through_dotdot",
            ["--source", "src/models.fe", "--source", "src/../src/models.fe", *stdout],
            stderr="src/../src/models.fe: error: source given twice\n",
        ),
        cli(
            "out_equal_to_a_source",
            ["--source", "src/models.fe", "--out", "src/models.fe"],
            stderr="error: the output must be a different module beside the sources\n",
        ),
        cli(
            "out_equal_to_a_source_by_another_spelling",
            ["--source", "src/models.fe", "--out", "./src/../src/models.fe"],
            stderr="error: the output must be a different module beside the sources\n",
        ),
        cli(
            "out_equal_to_a_source_with_check",
            ["--source", "src/models.fe", "--out", "src/models.fe", "--check"],
            stderr="error: the output must be a different module beside the sources\n",
        ),
        cli(
            "out_equal_to_the_second_source",
            ["--source", "src/models.fe", "--source", "src/more.fe", "--out", "src/more.fe"],
            files={"src/more.fe": GOOD.replace("Item", "More")},
            stderr="error: the output must be a different module beside the sources\n",
        ),
        cli(
            "out_in_another_ingot",
            ["--source", "src/models.fe", "--out", "other/src/y.fe"],
            files=OTHER_INGOT,
            stderr="error: the output must be a different module beside the sources\n",
        ),
        cli(
            "out_in_another_ingot_with_the_name_of_a_source",
            ["--source", "src/models.fe", "--out", "other/src/models.fe"],
            files=OTHER_INGOT,
            stderr="error: the output must be a different module beside the sources\n",
        ),
        cli(
            "out_error_comes_before_a_missing_source",
            ["--source", "src/missing.fe", "--out", "other/src/y.fe"],
            files=OTHER_INGOT,
            stderr="error: the output must be a different module beside the sources\n",
        ),
        cli(
            "ingot_error_comes_before_a_missing_source",
            ["--source", "src/missing.fe", "--source", "other/src/x.fe", *stdout],
            files=OTHER_INGOT,
            stderr="error: every --source must be in the same ingot's src directory\n",
        ),
    ]

    # Reading sources.
    cases += [
        cli(
            "missing_source_file",
            ["--source", "src/missing.fe", *stdout],
            stderr="src/missing.fe: error: No such file or directory (os error 2)\n",
        ),
        cli(
            "missing_source_directory",
            ["--source", "nodir/models.fe", *stdout],
            stderr="nodir: error: No such file or directory (os error 2)\n",
        ),
        cli(
            "missing_output_directory",
            ["--source", "src/models.fe", "--out", "nodir/x.fe"],
            stderr="nodir: error: No such file or directory (os error 2)\n",
        ),
        cli(
            "source_is_a_directory",
            ["--source", "src/dir.fe", *stdout],
            files={"src/dir.fe/keep": ""},
            stderr="src/dir.fe: error: Is a directory (os error 21)\n",
        ),
        cli(
            "missing_second_source",
            ["--source", "src/models.fe", "--source", "src/missing.fe", *stdout],
            stderr="src/missing.fe: error: No such file or directory (os error 2)\n",
        ),
        cli(
            "missing_source_is_reported_before_a_syntax_error",
            ["--source", "src/bad.fe", "--source", "src/missing.fe", *stdout],
            files={"src/bad.fe": "pub struct {\n"},
            stderr="src/missing.fe: error: No such file or directory (os error 2)\n",
        ),
        cli(
            "source_not_utf8",
            ["--source", "src/models.fe", *stdout],
            files={"src/models.fe": b"\xff\xfe\n"},
            stderr="src/models.fe: error: stream did not contain valid UTF-8\n",
        ),
        cli(
            "source_not_utf8_after_valid_text",
            ["--source", "src/models.fe", *stdout],
            files={"src/models.fe": GOOD.encode() + b"// \xc3\x28\n"},
            stderr="src/models.fe: error: stream did not contain valid UTF-8\n",
        ),
        cli(
            "source_not_utf8_truncated_sequence",
            ["--source", "src/models.fe", *stdout],
            files={"src/models.fe": GOOD.encode() + b"// \xe2\x82"},
            stderr="src/models.fe: error: stream did not contain valid UTF-8\n",
        ),
        cli(
            "source_not_utf8_overlong",
            ["--source", "src/models.fe", *stdout],
            files={"src/models.fe": GOOD.encode() + b"// \xc0\xaf\n"},
            stderr="src/models.fe: error: stream did not contain valid UTF-8\n",
        ),
        cli(
            "source_not_utf8_surrogate",
            ["--source", "src/models.fe", *stdout],
            files={"src/models.fe": GOOD.encode() + b"// \xed\xa0\x80\n"},
            stderr="src/models.fe: error: stream did not contain valid UTF-8\n",
        ),
        cli(
            "source_not_utf8_second_source",
            ["--source", "src/models.fe", "--source", "src/bad.fe", *stdout],
            files={"src/bad.fe": b"\x80"},
            stderr="src/bad.fe: error: stream did not contain valid UTF-8\n",
        ),
        cli(
            "source_not_utf8_is_reported_before_a_syntax_error",
            ["--source", "src/bad.fe", "--source", "src/worse.fe", *stdout],
            files={"src/bad.fe": "pub struct {\n", "src/worse.fe": b"\xff"},
            stderr="src/worse.fe: error: stream did not contain valid UTF-8\n",
        ),
        cli(
            "empty_source_has_nothing_to_generate",
            ["--source", "src/empty.fe", *stdout],
            files={"src/empty.fe": ""},
            stderr="src/empty.fe:1:1: error: no #[serializable] struct or enum found\n",
        ),
    ]
    cases.append(
        cli(
            "stdout_cannot_be_written",
            ["--source", "src/models.fe", *stdout],
            stderr_prefix="cannot write generated source: ",
            close_stdout=True,
        )
    )
    # Usage errors: clap's exit status 2 and a message that starts with `error:`.
    for label, args, needles in [
        ("no_source", ["--stdout"], ("--source <SOURCE>",)),
        ("no_out_and_no_stdout", ["--source", "src/models.fe"], ("--out <OUT>",)),
        ("no_arguments", [], ("--source <SOURCE>", "--out <OUT>")),
        ("stdout_with_out", ["--source", "src/models.fe", "--stdout", "--out", "src/x.fe"], ("--stdout", "--out", "cannot be used with")),
        ("stdout_with_check", ["--source", "src/models.fe", "--stdout", "--check"], ("--stdout", "--check", "cannot be used with")),
        ("check_without_out", ["--source", "src/models.fe", "--check"], ("--out <OUT>",)),
        ("unknown_flag", ["--source", "src/models.fe", "--stdout", "--bogus"], ("--bogus",)),
        ("unknown_short_flag", ["--source", "src/models.fe", "--stdout", "-x"], ("-x",)),
        ("source_without_a_value", ["--stdout", "--source"], ("--source",)),
        ("out_without_a_value", ["--source", "src/models.fe", "--out"], ("--out",)),
        ("out_twice", ["--source", "src/models.fe", "--out", "src/a.fe", "--out", "src/b.fe"], ("--out", "cannot be used multiple times")),
        ("stdout_twice", ["--source", "src/models.fe", "--stdout", "--stdout"], ("--stdout", "cannot be used multiple times")),
        ("positional_argument", ["--source", "src/models.fe", "--stdout", "src/models.fe"], ("src/models.fe",)),
        ("value_for_a_flag", ["--source", "src/models.fe", "--stdout=1"], ("--stdout",)),
    ]:
        cases.append(
            cli(
                f"usage_{label}",
                args,
                status=2,
                stderr_prefix="error:",
                stderr_contains=needles,
            )
        )

    version = "fe-codec-gen 0.1.0\n"
    for flag in ("--version", "-V"):
        cases.append(cli(f"version_{flag.lstrip('-')}", [flag], status=0, stdout=version, stderr=""))
    cases.append(cli("version_with_other_arguments", ["--source", "src/models.fe", "--stdout", "--version"], status=0, stdout=version, stderr=""))
    for flag in ("--help", "-h"):
        cases.append(
            cli(
                f"help_{flag.lstrip('-')}",
                [flag],
                status=0,
                stderr="",
                stdout=None,
                stdout_prefix="Generate Fe Codec impls for #[serializable] structs and enums",
                stdout_contains=("Usage: fe-codec-gen", "--source <SOURCE>", "--out <OUT>", "--stdout", "--check", "--eip712", "--help", "--version"),
            )
        )

    # Spellings that must work.
    plain = ["--source", "src/models.fe", "--stdout"]
    cases += [
        cli("source_with_dot_slash", ["--source", "./src/models.fe", *stdout], status=0, stdout=None, stderr="", same_as=plain),
        cli("source_through_dotdot", ["--source", "src/../src/models.fe", *stdout], status=0, stdout=None, stderr="", same_as=plain),
        cli("source_absolute", ["--source", "{ws}/src/models.fe", *stdout], status=0, stdout=None, stderr="", same_as=plain),
        cli("source_equals_form", ["--source=src/models.fe", *stdout], status=0, stdout=None, stderr="", same_as=plain),
        cli("options_before_source", [*stdout, "--source", "src/models.fe"], status=0, stdout=None, stderr="", same_as=plain),
        cli(
            "source_that_is_a_symlink_to_a_file",
            ["--source", "src/link.fe", *stdout],
            status=0,
            stdout=None,
            stderr="",
            setup=lambda ws: os.symlink("models.fe", ws / "src" / "link.fe"),
        ),
        cli(
            "out_equals_form",
            ["--source", "src/models.fe", "--out=src/serializers.fe"],
            status=0,
            stderr="",
            changes=True,
            writes={"src/serializers.fe": "generated"},
        ),
        cli(
            "out_with_dot_slash_and_dotdot",
            ["--source", "src/models.fe", "--out", "./src/../src/serializers.fe"],
            status=0,
            stderr="",
            changes=True,
            writes={"src/serializers.fe": "generated"},
        ),
        cli(
            "out_absolute",
            ["--source", "src/models.fe", "--out", "{ws}/src/serializers.fe"],
            status=0,
            stderr="",
            changes=True,
            writes={"src/serializers.fe": "generated"},
        ),
        cli(
            "out_in_a_source_directory_with_several_sources",
            ["--source", "src/models.fe", "--source", "src/more.fe", "--out", "src/serializers.fe"],
            files={"src/more.fe": GOOD.replace("Item", "More")},
            status=0,
            stderr="",
            changes=True,
            writes={"src/serializers.fe": None},
        ),
        cli(
            "out_a_dangling_symlink",
            ["--source", "src/models.fe", "--out", "src/serializers.fe"],
            stderr=NOT_REGULAR,
            setup=lambda ws: os.symlink("nowhere.fe", ws / "src" / "serializers.fe"),
        ),
        cli(
            "out_a_dangling_symlink_with_check",
            ["--source", "src/models.fe", "--out", "src/serializers.fe", "--check"],
            stderr=NOT_REGULAR,
            setup=lambda ws: os.symlink("nowhere.fe", ws / "src" / "serializers.fe"),
        ),
        cli(
            "out_a_symlink_to_a_directory",
            ["--source", "src/models.fe", "--out", "src/serializers.fe"],
            stderr=NOT_REGULAR,
            files={"elsewhere/keep": ""},
            setup=lambda ws: os.symlink("../elsewhere", ws / "src" / "serializers.fe"),
        ),
    ]
    return cases


def expand(text, ws: Path):
    if isinstance(text, str):
        return text.replace("{ws}", str(ws))
    return text


def write_files(ws: Path, files: dict) -> None:
    for relative, content in files.items():
        path = ws / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode("utf-8")
        path.write_bytes(content)


def run_cli_case(case: CliCase, report: Report, keep: bool, kept: list) -> None:
    print(f"cli_table/{case.name}")
    ws = Path(tempfile.mkdtemp(prefix=f"cli-{case.name[:30]}-"))
    if keep:
        kept.append(ws)
    try:
        write_files(ws, {} if case.bare else BASE_FILES)
        write_files(ws, case.files or {})
        if case.setup:
            case.setup(ws)
        args = [expand(arg, ws) for arg in case.args]
        reference = run_generator(ws, ["--source", "src/models.fe", "--stdout"])
        before = snapshot(ws)
        step = run_generator(ws, args, close_stdout=case.close_stdout)
        name = f"cli_table/{case.name}"
        problems = []
        if step.status != case.status:
            problems.append(f"exit status: expected {case.status}, got {step.status}")
        if case.stderr is not None:
            problems.append(same_text(step.stderr, expand(case.stderr, ws)))
        if case.stderr_prefix is not None and not step.stderr.startswith(case.stderr_prefix):
            problems.append(f"stderr should start with {show(case.stderr_prefix)}, got {show(step.stderr)}")
        for needle in case.stderr_contains:
            if needle not in step.stderr:
                problems.append(f"stderr lacks {show(needle)}: {show(step.stderr)}")
        if isinstance(case.stdout, bytes) and step.stdout != case.stdout:
            problems.append(f"stdout: expected {case.stdout!r}, got {step.stdout[:80]!r}")
        if isinstance(case.stdout, str) and step.stdout.decode("utf-8", "replace") != case.stdout:
            problems.append(f"stdout: expected {case.stdout!r}, got {step.stdout[:80]!r}")
        text = step.stdout.decode("utf-8", errors="replace")
        if case.stdout_prefix is not None and not text.startswith(case.stdout_prefix):
            problems.append(f"stdout should start with {show(case.stdout_prefix)}")
        for needle in case.stdout_contains:
            if needle not in text:
                problems.append(f"stdout lacks {show(needle)}")
        if case.same_as is not None:
            other = run_generator(ws, case.same_as)
            if other.status != 0 or step.stdout != other.stdout:
                problems.append("stdout differs from the plain --stdout run")
        if case.status == 0 and case.stdout is None and case.stderr == "" and not step.stdout and not case.writes:
            problems.append("stdout is empty")
        if case.writes is not None:
            for relative in case.writes:
                path = ws / relative
                if not path.is_file():
                    problems.append(f"{relative} was not written")
                elif case.writes[relative] == "generated" and path.read_bytes() != reference.stdout:
                    problems.append(f"{relative} differs from what --stdout prints")
        if not case.changes:
            problem = diff_snapshots(before, snapshot(ws))
            if problem:
                problems.append(problem)
        report.check(case.name, name, "\n".join(p for p in problems if p) or None)
    finally:
        if not keep:
            shutil.rmtree(ws, ignore_errors=True)


# ---------------------------------------------------------------------------------------------
# Message coverage


def strip_comments(text: str) -> str:
    return re.sub(r"//[^\n]*", lambda m: " " * len(m.group(0)), text)


def matching_paren(text: str, open_index: int) -> int:
    depth = 0
    in_string = False
    i = open_index
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced")


def split_arguments(text: str) -> list:
    arguments, current, depth, in_string, i = [], [], 0, False, 0
    while i < len(text):
        ch = text[i]
        if in_string:
            current.append(ch)
            if ch == "\\":
                current.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            current.append(ch)
        elif ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            arguments.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail:
        arguments.append(tail)
    return arguments


def format_message(arguments: list) -> tuple | None:
    """(display text, regex) of a `format!` argument list, or None for the field wrapper."""
    if arguments and not arguments[-1].startswith('"') and arguments[-1] == "message":
        return None
    display, pattern = [], []
    for argument in arguments:
        if argument.startswith('"'):
            literal = argument[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            display.append(literal)
            pattern.append(re.escape(literal))
        else:
            display.append("{}")
            pattern.append(".+")
    return "".join(display), "".join(pattern)


def extract_messages(path: Path) -> list:
    """Every diagnostic message the Fe source can produce: (display, regex, file:line)."""
    original = path.read_text(encoding="utf-8")
    text = strip_comments(original)
    found = []

    def add(start: int, arguments: list, inner: bool) -> None:
        message = format_message(arguments)
        if message is None:
            return
        display, pattern = message
        if inner:
            display = "field '{}': " + display
            pattern = "field '[^']*': " + pattern
        line = original.count("\n", 0, start) + 1
        found.append((display, pattern, f"{path.name}:{line}"))

    for match in re.finditer(r"(?<![\w:])(?:fail|error)\(", text):
        end = matching_paren(text, match.end() - 1)
        arguments = split_arguments(text[match.end() : end])
        if len(arguments) >= 3 and arguments[2].startswith("format!("):
            inner = split_arguments(arguments[2][len("format!(") : -1])
            add(match.start(), inner, False)
    for match in re.finditer(r"Result::Err\(\s*format!\(", text):
        end = matching_paren(text, match.end() - 1)
        add(match.start(), split_arguments(text[match.end() : end]), True)
    for match in re.finditer(r"message:\s*format!\(", text):
        end = matching_paren(text, match.end() - 1)
        add(match.start(), split_arguments(text[match.end() : end]), False)
    return found


def message_of(stderr: str) -> str | None:
    match = DIAGNOSTIC.match(stderr)
    return match.group("message") if match else None


def coverage(cases: list, report: Report) -> None:
    table: dict = {}
    for path in MESSAGE_SOURCES:
        for display, pattern, where in extract_messages(path):
            table.setdefault(display, {"pattern": re.compile(pattern), "where": [], "cases": []})["where"].append(where)
    expectations = []
    for case in cases:
        for text in (case.expect_stderr, case.break_stderr):
            message = message_of(text) if text else None
            if message is not None and case.name not in report.failed_cases:
                expectations.append((case.name, message))
    for display, entry in table.items():
        for name, message in expectations:
            if entry["pattern"].fullmatch(message) and name not in entry["cases"]:
                entry["cases"].append(name)

    print("\nMessage coverage (tools/codegen/src/schema.fe, eip712.fe)")
    missing = []
    for display, entry in table.items():
        names = entry["cases"]
        if names:
            status = f"{len(names)} case(s): " + ", ".join(names[:3]) + (", ..." if len(names) > 3 else "")
        elif display in UNREACHABLE:
            status = "unreachable: " + UNREACHABLE[display]
        else:
            status = "NO CASE"
            missing.append(display)
        print(f"  {display}\n      {', '.join(entry['where'])}  ->  {status}")
    for display in UNREACHABLE:
        if display not in table:
            report.check("coverage", f"coverage: UNREACHABLE entry {display!r} is not a message in the sources", "stale entry")
        elif table[display]["cases"]:
            report.check("coverage", f"coverage: UNREACHABLE entry {display!r} is covered by a case", "remove it from UNREACHABLE")
    report.check(
        "coverage",
        f"coverage: every message has a case or is listed as unreachable ({len(table)} messages)",
        None if not missing else "no case for:\n" + "\n".join(missing),
    )


# ---------------------------------------------------------------------------------------------


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Specification tests for the Fe code generator's command line and diagnostics.")
    parser.add_argument("--case", help="run only the corpus case or command line case with this name")
    parser.add_argument("--keep", action="store_true", help="keep temporary directories and print their paths")
    args = parser.parse_args(argv)

    warm = subprocess.run([str(GENERATOR), "--version"], capture_output=True, env=ENV, check=False)
    if warm.returncode != 0:
        sys.stderr.write(warm.stderr.decode("utf-8", errors="replace"))
        raise SystemExit("tools/fe-codec-gen does not build")

    cases = load_cases(None)
    cli_cases = build_cli_cases()
    selected = [c for c in cases if not args.case or c.name == args.case]
    selected_cli = [c for c in cli_cases if not args.case or c.name == args.case or f"cli_table/{c.name}" == args.case]
    if args.case and not selected and not selected_cli:
        raise SystemExit(f"no case named {args.case!r}")

    report = Report()
    kept: list = []
    for case in selected:
        run_case(case, report, args.keep, kept)
    for case in selected_cli:
        run_cli_case(case, report, args.keep, kept)
    if not args.case:
        coverage(cases, report)

    kinds: dict = {}
    for case in selected:
        kinds[case.kind] = kinds.get(case.kind, 0) + 1
    summary = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
    print(
        f"\ncorpus cases: {len(selected)} ({summary}), command line cases: {len(selected_cli)}, "
        f"checks: {report.passed + report.failed + report.xfailed}, passed: {report.passed}, "
        f"failed: {report.failed}, expected failures: {report.xfailed}"
    )
    if kept:
        print("kept temporary directories:")
        for ws in kept:
            print(f"  {ws}")
    if report.known:
        print(f"known defects ({report.xfailed} expected failures in {len(report.known)} cases):")
        for name, text in sorted(report.known.items()):
            print(f"  {name}: {text}")
    if report.failed_cases:
        print("failing cases: " + ", ".join(sorted(report.failed_cases)))
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
