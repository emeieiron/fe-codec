"""Exercise a clean downstream FeMP project against pinned Fe Codec ingots."""

import argparse
import json
import tomllib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples/transfer"


def run(command, cwd, env=None, **kwargs):
    return subprocess.run(command, cwd=cwd, env=env, check=True, **kwargs)


def fixture_release(root):
    repo = root / "release"
    repo.mkdir()
    shutil.copy(ROOT / "fe.toml", repo)
    for name in ("core", "formats", "io", "tools"):
        shutil.copytree(ROOT / name, repo / name, ignore=shutil.ignore_patterns("out", "build"))
    run(["git", "init", "-q"], repo)
    run(["git", "add", "."], repo)
    run(
        ["git", "-c", "user.name=Fe Codec Tests", "-c", "user.email=tests@example.invalid",
         "commit", "-qm", "Fe Codec consumer fixture"],
        repo,
    )
    rev = run(["git", "rev-parse", "HEAD"], repo, capture_output=True, text=True).stdout.strip()
    return repo.as_uri(), rev


def consumer_project(app, generator, source, rev):
    ignore = shutil.ignore_patterns("build", "dist", "serializers.fe", "regressions")
    shutil.copytree(DEMO, app, ignore=ignore)
    # The demo's third-party format sits beside it, as an ordinary ingot of the consumer's.
    shutil.copytree(ROOT / "examples/compact_format", app.parent / "compact_format", ignore=ignore)
    manifest = app / "femp.toml"
    text = manifest.read_text()
    start, stop = text.index("[modules.io]"), text.index("[modules.compact]")
    text = text[:start] + text[stop:]
    # The demo runs each generator through the tools/fe_codec_gen wrapper; keep its arguments,
    # everything after the wrapper.
    demo = tomllib.loads((DEMO / "femp.toml").read_text())
    for name, section in demo["modules"]["shared"]["generate"].items():
        header = text.index(f"[modules.shared.generate.{name}]")
        start = text.index("command = [", header)
        stop = text.index("output =", start)
        original = section["command"]
        command = [generator, *original[1:]]
        text = text[:start] + f"command = {json.dumps(command)}\n" + text[stop:]
    manifest.write_text(text)

    for ingot in [app / module / "fe.toml" for module in ("shared", "native", "wasm")] + [
        app.parent / "compact_format/fe.toml"
    ]:
        lines = []
        for line in ingot.read_text().splitlines():
            alias = line.partition("=")[0].strip()
            if alias.startswith("fe_codec_"):
                source_value, rev_value, name = map(json.dumps, (source, rev, alias))
                line = f"{alias} = {{ source = {source_value}, rev = {rev_value}, name = {name} }}"
            lines.append(line)
        ingot.write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--femp", default="femp")
    parser.add_argument(
        "--generator", default="fe_codec_gen",
        help="the fe_codec_gen executable",
    )
    parser.add_argument("--source", help="Git repository for a published release")
    parser.add_argument("--rev", help="Full Git revision of the release")
    args = parser.parse_args()
    if bool(args.source) != bool(args.rev):
        parser.error("--source and --rev must be provided together")
    femp, generator = shutil.which(args.femp), shutil.which(args.generator)
    if not femp or not generator:
        parser.error("provide the paths to built femp and generator executables")
    femp, generator = str(Path(femp).resolve()), str(Path(generator).resolve())

    with tempfile.TemporaryDirectory(prefix="fe-codec-consumer-") as directory:
        root = Path(directory).resolve()
        source, rev = (args.source, args.rev) if args.source else fixture_release(root)
        # Keep the consumer independent of previous checkouts and cached revisions.
        env = {**os.environ, "FE_REMOTE_CACHE_DIR": str(root / "git-cache")}
        app = root / "consumer"
        consumer_project(app, generator, source, rev)
        targets = ["-t", "evm", "-t", "native", "-t", "wasm"]
        run([generator, "--version"], app, env)
        run([femp, "check", *targets], app, env)
        # The ID is unchanged, but generation must pick up the new Fe variant name.
        model = app / "shared/src/models.fe"
        original = model.read_text()
        assert "Waiting," in original
        model.write_text(original.replace("Waiting,", "Queued,"))
        for command in ("test", "package"):
            run([femp, command, *targets], app, env)
        assert not (app / "shared/src/serializers.fe").exists()

        index = json.loads((app / "dist/manifest.json").read_text())["packages"]
        assert {package["platform"] for package in index} == {"evm", "native", "wasm"}
        evm = next(package for package in index if package["platform"] == "evm")
        code = next(item["path"] for item in evm["files"] if item["kind"] == "runtime-bytecode")
        assert len(bytes.fromhex((app / "dist" / code).read_text().strip())) <= 24_576
        native = next(package for package in index if package["platform"] == "native")
        executable = next(item["path"] for item in native["files"] if item["kind"] == "executable")
        command = [str(app / "dist" / executable)]
        assert run([*command, '{"value":42}'], app, env, capture_output=True).stdout == b'{"value":42}\n'
        assert run([*command, "--cbor", '{"value":42}'], app, env, capture_output=True).stdout == bytes.fromhex("a101182a")
        failed = subprocess.run([*command, "{}"], capture_output=True)
        assert failed.returncode == 1 and b"field value" in failed.stderr
        run(
            ["node", "--input-type=module", "-e", """
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { CodecTransferWasm } from './dist/wasm/web/codec_transfer_wasm.js';
const bytes = await readFile('./dist/wasm/web/codec_transfer_wasm.wasm');
const codec = await CodecTransferWasm.load(bytes);
assert.deepEqual(codec.decodeJsonValue('{"value":42}'), { ok: true, value: 42 });
assert.deepEqual(codec.decodeJsonValue('{}'), { ok: false, code: 9 });
"""],
            app,
            env,
        )


if __name__ == "__main__":
    main()
