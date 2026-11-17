"""Check that Fe Codec works with the official Fe toolchain alone.

Copies the runtime ingots and examples/vanilla into a temporary directory with no FeMP
manifest, checks that the committed generated module is current, and runs the example's
tests on each `fe test` backend. `fe` and the generator must be on PATH, or pass --fe and
--generator.
"""

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
INGOTS = ("io", "core", "formats/json", "formats/cbor")
BACKENDS = ("sonatina", "native")


def run(command, cwd):
    print("+", " ".join(str(part) for part in command), flush=True)
    return subprocess.run(command, cwd=cwd, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fe", default="fe", help="the Fe compiler to use")
    parser.add_argument(
        "--generator", default="fe_codec_gen",
        help="the generator: fe_codec_gen, or the Rust reference fe-codec-gen",
    )
    args = parser.parse_args()
    fe, generator = shutil.which(args.fe), shutil.which(args.generator)
    if not fe or not generator:
        parser.error("provide the paths to the fe and generator executables")
    fe, generator = str(Path(fe).resolve()), str(Path(generator).resolve())

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ignore = shutil.ignore_patterns("build", "out", "dist", "target")
        for ingot in INGOTS:
            shutil.copytree(ROOT / ingot, root / ingot, ignore=ignore)
        app = root / "examples/vanilla"
        shutil.copytree(ROOT / "examples/vanilla", app, ignore=ignore)
        if list(root.rglob("femp.toml")):
            raise SystemExit("the vanilla check must not see a FeMP manifest")

        run([fe, "--version"], app)
        run(
            [generator, "--source", "src/models.fe", "--out", "src/serializers.fe", "--check"],
            app,
        )
        for backend in BACKENDS:
            run([fe, "test", "--backend", backend], app)
    print("vanilla Fe consumer: ok")


if __name__ == "__main__":
    main()
