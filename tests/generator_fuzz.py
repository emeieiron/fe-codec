#!/usr/bin/env python3
"""Randomized end-to-end tests for fe_codec_gen, with no reference implementation and no stored
golden outputs.

For each seed it builds a random model spec, writes it as Fe source with formatting noise, runs
`tools/fe_codec_gen` on it, and tests the generated serializers and EIP-712 hashing with `fe test`.
Every expected value comes from outside the generator:

  * the wire formats of docs/formats.md, implemented in tests/vectors/reference.py;
  * EIP-712 as transcribed in tests/vectors/eip712.py;
  * the Fe compiler, which must accept what the generator writes;
  * relations between runs of the generator: the same declarations, rewritten with other
    whitespace, other line endings or a different field order, must give byte-identical output.

Usage: python3 tests/generator_fuzz.py [--seeds N] [--seed BASE] [--keep]
                                       [--backend native|sonatina|both] [--jobs J]
"""

import argparse
import collections
import concurrent.futures
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from fuzz import coverage  # noqa: E402
from fuzz.fetests import Builder  # noqa: E402
from fuzz.gen import make_spec  # noqa: E402
from fuzz.source import Renderer, finalize, mix_fill, permutation  # noqa: E402
from fuzz.spec import closure_models, walk  # noqa: E402

GENERATOR = os.path.join(ROOT, "tools", "fe_codec_gen")
DEFAULT_SEEDS = 20
TIMEOUT = 900


def environment():
    env = dict(os.environ)
    env["PATH"] = os.path.expanduser("~/.fe/bin") + os.pathsep + env.get("PATH", "")
    return env


def run(cmd, cwd, timeout=TIMEOUT):
    p = subprocess.run(cmd, cwd=cwd, env=environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def gen_command(modules, eip712, mode):
    cmd = [GENERATOR]
    for m in modules:
        cmd += ["--source", "src/%s.fe" % m]
    if eip712:
        cmd.append("--eip712")
    return cmd + mode


FE_TOML = """[ingot]
name = "generator_fuzz"
version = "0.1.0"

[dependencies]
fe_codec_core = "{r}/core"
fe_codec_json = "{r}/formats/json"
fe_codec_cbor = "{r}/formats/cbor"
fe_codec_eip712 = "{r}/formats/eip712"
codec_compact = "{r}/examples/compact_format"
"""


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())


def write_sources(directory, files, scaffold=True):
    if scaffold:  # the generator wants sources that sit in an ingot's src directory
        write(os.path.join(directory, "fe.toml"), FE_TOML.format(r="."))
        write(os.path.join(directory, "src", "lib.fe"), "")
    for name, data in files.items():
        write(os.path.join(directory, "src", name + ".fe"), data)


class Failure(Exception):
    pass


class SeedResult:
    def __init__(self, seed):
        self.seed = seed
        self.failures = []  # (stage, text)
        self.directory = None
        self.timings = collections.OrderedDict()
        self.features = collections.Counter()
        self.tests = 0
        self.models = 0
        self.backend_summary = {}
        self.sabotaged = None

    def fail(self, stage, text):
        self.failures.append((stage, text))


def tail(data, lines=40):
    text = data.decode(errors="replace") if isinstance(data, bytes) else data
    rows = text.strip().split("\n")
    return "\n".join(rows[-lines:])


def failure_digest(output):
    """The FAIL lines of an `fe test` run with the reason that follows each, or the compile
    errors when nothing ran."""
    rows = output.split("\n")
    out = []
    for i, line in enumerate(rows):
        if line.startswith("FAIL"):
            out.append(line)
            out.extend(r for r in rows[i + 1:i + 4] if r.startswith("    ") and r.strip())
    return "\n".join(out[:60]) if out else tail(output, 60)


def parse_failed_tests(output):
    failed = []
    for line in output.split("\n"):
        m = re.match(r"^FAIL\s+\[[^\]]*\]\s+(\S+)", line)
        if m:
            failed.append(m.group(1))
    return failed


FE_PARSE_ERROR = re.compile(r"lib\.fe:(\d+):(\d+)")
GENERATOR_ERROR = re.compile(r"^(src/[^:]+\.fe):(\d+):(\d+): error: ")


def fe_check_parse_error(path):
    """(line, column) of the first parse error plain `fe check` reports for the file at `path`,
    or None when it parses."""
    with tempfile.TemporaryDirectory(prefix="fuzz-fe-check-") as tmp:
        os.mkdir(os.path.join(tmp, "src"))
        with open(os.path.join(tmp, "fe.toml"), "w") as f:
            f.write('[ingot]\nname = "probe"\nversion = "0.1.0"\n')
        shutil.copy(path, os.path.join(tmp, "src", "lib.fe"))
        _, out, err = run(["fe", "check", tmp], tmp)
    rows = (out + err).decode(errors="replace").split("\n")
    positions = []
    for i, row in enumerate(rows):
        if row.startswith("error[1-") and i + 1 < len(rows):
            m = FE_PARSE_ERROR.search(rows[i + 1])
            if m:
                positions.append((int(m.group(1)), int(m.group(2))))
    return min(positions) if positions else None


def rejected_as_fe_check_does(err, directory):
    """Whether the generator's syntax error names the file and position where `fe check` reports
    the first parse error, so the variant is malformed Fe rather than misread."""
    m = GENERATOR_ERROR.match(err.decode(errors="replace"))
    if not m:
        return False
    position = fe_check_parse_error(os.path.join(directory, m.group(1)))
    return position == (int(m.group(2)), int(m.group(3)))


def metamorphic(result, spec, seed, base_files, crlf_flags, modules, workdir, eip712, args):
    """Variants of the same declarations must give the same generator output. Variant (d) also
    puts newlines in gaps where the compiler accepts or rejects them depending on what follows;
    where it is malformed, the generator must reject it exactly where `fe check` does."""
    rng = random.Random("%s/meta" % seed)
    renderer = Renderer(spec, seed)
    base_src = renderer.render()

    def finals(src_map, crlf=None, fill=None):
        crlf = crlf_flags if crlf is None else crlf
        return {name: finalize(s, crlf[name], fill) for name, s in src_map.items()}

    permuted = permutation(spec, rng)
    variants = {
        "(a) whitespace": finals(base_src, fill=mix_fill(rng)),
        "(b) line endings": finals(base_src, crlf={n: not crlf_flags[n] for n in modules}),
        "(c) field order": finals(Renderer(spec, seed, permuted).render()),
        "(d) newlines": finals(base_src, fill=mix_fill(rng, newline_gaps=True)),
    }
    if permuted:
        result.features["metamorphic: field permutation"] += 1
    modes = [(False, "serializers")] + ([(True, "eip712")] if eip712 else [])
    for is_eip, label in modes:
        cmd = gen_command(modules, is_eip, ["--stdout"])
        rc, base_out, err = run(cmd, os.path.join(workdir, "ingot"))
        if rc != 0:
            result.fail("metamorphic", "base --stdout (%s) failed: %s" % (label, tail(err)))
            continue
        written = os.path.join(workdir, "ingot", "src", "eip712.fe" if is_eip else "serializers.fe")
        with open(written, "rb") as f:
            if f.read() != base_out:
                result.fail("metamorphic", "--stdout differs from the --out file (%s)" % label)
        for name, files in variants.items():
            d = os.path.join(workdir, "variant " + name.split()[0].strip("()"))
            shutil.rmtree(d, ignore_errors=True)
            write_sources(d, files)
            rc, out, err = run(cmd, d)
            if rc != 0:
                if name.startswith("(d)") and rejected_as_fe_check_does(err, d):
                    result.features["metamorphic: newline variant rejected where fe check rejects it"] += 1
                    continue
                result.fail("metamorphic", "%s: generator rejected the variant (%s): %s\nvariant sources are in %s"
                            % (name, label, tail(err, 10), d))
            elif out != base_out:
                result.fail("metamorphic", "%s: output differs from the base (%s); variant sources are in %s"
                            % (name, label, d))
            elif name.startswith("(d)"):
                result.features["metamorphic: newline variant accepted"] += 1
    # --check must notice a stale or missing module.
    d = os.path.join(workdir, "stale")
    shutil.rmtree(d, ignore_errors=True)
    write_sources(d, base_files)
    cmd = gen_command(modules, False, ["--out", "src/serializers.fe", "--check"])
    rc, _, _ = run(cmd, d)
    if rc == 0:
        result.fail("check", "--check passed although the output module is missing")
    run(gen_command(modules, False, ["--out", "src/serializers.fe"]), d)
    with open(os.path.join(d, "src", "serializers.fe"), "ab") as f:
        f.write(b"\n// stale\n")
    rc, _, _ = run(cmd, d)
    if rc == 0:
        result.fail("check", "--check passed on a stale output module")


def sabotage(ingot, kind, result):
    """Damage the generated modules, so that the run shows the tests notice (a self-check of the
    suite: every seed must then fail)."""
    if kind == "hash":
        path, pattern, repl = "eip712.fe", r'core::keccak\("([A-Za-z])', lambda m: 'core::keccak("' + ("Q" if m.group(1) != "Q" else "R")
    elif kind == "id":
        path, pattern, repl = "serializers.fe", r"id: (\d+) \}", lambda m: "id: %d }" % (int(m.group(1)) + 1)
    else:  # name
        path, pattern, repl = "serializers.fe", r'schema_name<(\d+)>\("([A-Za-z])', \
            lambda m: 'schema_name<%s>("%s' % (m.group(1), "Q" if m.group(2) != "Q" else "R")
    full = os.path.join(ingot, "src", path)
    if not os.path.exists(full):
        result.sabotaged = False
        return
    with open(full) as f:
        text = f.read()
    new, n = re.subn(pattern, repl, text, count=1)
    result.sabotaged = n == 1
    with open(full, "w") as f:
        f.write(new)


def check_seed(seed, args):
    result = SeedResult(seed)
    start = time.time()
    workdir = tempfile.mkdtemp(prefix="fuzz-%d-" % seed)
    result.directory = workdir
    ingot = os.path.join(workdir, "ingot")
    try:
        spec = make_spec(random.Random("spec/%d" % seed))
        result.models = len(spec.models)
        modules = [chr(ord("a") + i) for i in range(spec.modules)]
        crng = random.Random("crlf/%d" % seed)
        crlf_flags = {n: crng.random() < 0.3 for n in modules}
        renderer = Renderer(spec, seed)
        src_map = renderer.render()
        files = {n: finalize(s, crlf_flags[n]) for n, s in src_map.items()}
        eip712 = any(m.eip712 for m in spec.models)

        rel = os.path.relpath(os.path.realpath(ROOT), os.path.realpath(ingot))
        write(os.path.join(ingot, "fe.toml"), FE_TOML.format(r=rel))
        write(os.path.join(ingot, "src", "lib.fe"), "//! Generated by tests/generator_fuzz.py.\n")
        write_sources(ingot, files, scaffold=False)

        # 1. Generate and check.
        t0 = time.time()
        for is_eip, out in [(False, "serializers"), (True, "eip712")][: 2 if eip712 else 1]:
            rc, so, se = run(gen_command(modules, is_eip, ["--out", "src/%s.fe" % out]), ingot)
            if rc != 0:
                result.fail("generate", "%s failed: %s" % ("--eip712" if is_eip else "generator", tail(se)))
                return result
            rc, so, se = run(gen_command(modules, is_eip, ["--out", "src/%s.fe" % out, "--check"]), ingot)
            if rc != 0:
                result.fail("check", "--check failed right after generating: %s" % tail(se))
        result.timings["generate"] = time.time() - t0
        if args.sabotage != "none":
            sabotage(ingot, args.sabotage, result)

        # 2. The test module.
        builder = Builder(spec, random.Random("values/%d" % seed), seed)
        write(os.path.join(ingot, "src", "tests.fe"), builder.build())
        result.tests = builder.stats["tests"]

        # 3. Fe tests.
        backends = ["native", "sonatina"] if args.backend == "both" else [args.backend]
        for backend in backends:
            t0 = time.time()
            # A loaded machine can make a native test cross the default 60 s limit.
            base = ["fe", "test"] + (["--backend", "native", "--native-timeout-secs", "900"]
                                     if backend == "native" else [])
            # The EVM backend deploys all the tests of a run as one contract, which has a size
            # limit: run its tests in batches (see MAX_BATCH_TESTS in tests/fuzz/fetests.py).
            def attempt(prefix):
                rc, so, se = run(base + (["--filter", prefix] if prefix else []), ingot)
                out = (so + se).decode(errors="replace")
                m = re.search(r"test result: (\w+)\. (\d+) passed; (\d+) failed", out)
                return rc, out, m

            ran, bad = 0, []
            for prefix in (builder.batches if backend == "sonatina" else [""]):
                rc, out, m = attempt(prefix)
                if "CreateContractSizeLimit" in out and len(builder.kinds.get(prefix, [])) > 1:
                    # Still too large: one run per kind of test in the batch.
                    runs = [(part,) + attempt(part) for part in builder.kinds[prefix]]
                else:
                    runs = [(prefix, rc, out, m)]
                for part, rc, out, m in runs:
                    if m:
                        ran += int(m.group(2))
                    if rc != 0 or not m or m.group(1) != "ok" or m.group(3) != "0":
                        failed = parse_failed_tests(out)
                        bad.append("batch %r: failing tests: %s\n%s" % (
                            part, ", ".join(failed) or "(compile errors)", failure_digest(out)))
            if not bad and ran != result.tests:
                bad.append("the runs together ran %d tests, expected %d" % (ran, result.tests))
            result.backend_summary[backend] = "test result: %s. %d of %d passed" % (
                "FAILED" if bad else "ok", ran, result.tests)
            if bad:
                result.fail("fe test (%s)" % backend, "\n".join(bad))
            result.timings["fe test " + backend] = time.time() - t0

        # 4. Metamorphic checks.
        t0 = time.time()
        if args.sabotage == "none":
            metamorphic(result, spec, seed, files, crlf_flags, modules, workdir, eip712, args)
        result.timings["metamorphic"] = time.time() - t0

        # 5. Coverage.
        extra = collections.Counter()
        if any(crlf_flags.values()):
            extra["source: CRLF"] += 1
        for m in spec.models:
            if m.eip712:
                root = ("ref", m.name, ())
                reached = [n for n in closure_models(spec, root) if spec.model(n).is_record]
                if len(reached) > 1:
                    extra["eip712: struct reached from a root"] += 1
                for n in reached:
                    for f in spec.model(n).fields:
                        if f.ty[0] == "arr" and f.ty[1][0] == "ref":
                            extra["eip712: array of structs"] += 1
                        if f.ty[0] == "arr" and f.ty[1][0] == "arr":
                            extra["eip712: nested arrays"] += 1
                        if any(w[0] == "list" for w in walk(f.ty)):
                            extra["eip712: list"] += 1
        if "missing_" in "".join(builder.fns):
            extra["test: missing required field"] += 1
        if "dropped_" in "".join(builder.fns):
            extra["test: dropped optional field"] += 1
        if builder.skipped_json:
            extra["skip: unknown JSON members"] += 1
        if builder.skipped_cbor:
            extra["skip: unknown CBOR fields"] += 1
        if "eip712_rejects_tuples_" in "".join(builder.fns):
            extra["test: tuples have no EIP-712 form"] += 1
        result.features = coverage.count(spec, renderer.stats, extra) + result.features
        return result
    except Exception as e:  # a bug in the harness is a failure too
        import traceback
        result.fail("harness", traceback.format_exc())
        return result
    finally:
        result.timings["total"] = time.time() - start
        if not result.failures and not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seeds", type=int, default=DEFAULT_SEEDS, help="how many seeds to run")
    parser.add_argument("--seed", type=int, default=1, help="the first seed")
    parser.add_argument("--keep", action="store_true", help="keep every temporary ingot")
    parser.add_argument("--backend", choices=["native", "sonatina", "both"], default="both")
    parser.add_argument("--sabotage", choices=["none", "id", "name", "hash"], default="none",
                        help="self-check: damage the generated modules; every seed must then fail")
    parser.add_argument("--jobs", type=int, default=2, help="seeds to check at once")
    args = parser.parse_args()

    if not os.path.exists(GENERATOR):
        sys.exit("missing " + GENERATOR)
    rc, _, err = run([GENERATOR, "--version"], ROOT)  # builds it once, before the seeds race to
    if rc != 0:
        sys.exit("building the generator failed:\n" + err.decode(errors="replace"))

    seeds = list(range(args.seed, args.seed + args.seeds))
    results = []
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(check_seed, s, args): s for s in seeds}
        for future in concurrent.futures.as_completed(futures):
            r = future.result()
            results.append(r)
            status = "FAIL" if r.failures else "ok"
            summary = "; ".join("%s: %s" % (b, s) for b, s in sorted(r.backend_summary.items()))
            print("seed %d: %s (%d models, %d tests, %.1fs: %s) [%s]" % (
                r.seed, status, r.models, r.tests, r.timings.get("total", 0),
                ", ".join("%s %.1fs" % (k, v) for k, v in r.timings.items() if k != "total"), summary),
                flush=True)
            for stage, text in r.failures:
                print("  FAILED %s (seed %d):\n%s\n  ingot kept at %s" % (
                    stage, r.seed, "\n".join("    " + l for l in text.split("\n")), r.directory), flush=True)

    results.sort(key=lambda r: r.seed)
    if args.sabotage != "none":
        undetected = [r.seed for r in results if r.sabotaged and not r.failures]
        skipped = [r.seed for r in results if not r.sabotaged]
        print("\nsabotage %r: %d seed(s) damaged and detected, %d not damaged (nothing to damage), %d UNDETECTED %s" % (
            args.sabotage, sum(1 for r in results if r.sabotaged and r.failures), len(skipped), len(undetected),
            undetected or ""))
        sys.exit(1 if undetected else 0)
    total = collections.Counter()
    for r in results:
        total.update(r.features)

    print("\nCoverage over %d seed(s):" % len(results))
    missing = []
    width = max(len(n) for n in coverage.REQUIRED)
    for name in coverage.REQUIRED:
        print("  %-*s %6d" % (width, name, total[name]))
        if total[name] == 0:
            missing.append(name)

    failed = [r for r in results if r.failures]
    elapsed = time.time() - started
    per_seed = sum(r.timings.get("total", 0) for r in results) / max(1, len(results))
    print("\n%d seed(s), %d passed, %d failed; %.0fs in all, %.1fs per seed (jobs=%d)" % (
        len(results), len(results) - len(failed), len(failed), elapsed, per_seed, args.jobs))
    for r in failed:
        print("  seed %d failed at: %s; ingot %s" % (r.seed, ", ".join(s for s, _ in r.failures), r.directory))
    if missing and args.seeds >= DEFAULT_SEEDS:
        print("features never exercised: " + ", ".join(missing))
        sys.exit(1)
    elif missing:
        print("(%d feature(s) not exercised; only enforced for %d or more seeds)" % (len(missing), DEFAULT_SEEDS))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
