#!/bin/sh
# Builds the playground's WebAssembly module and copies it into site/src/wasm/, which
# is committed so the site builds with Node alone. Needs fe and femp on PATH. Run it after
# changing tools/, the codec ingots or site/playground/, then run tests/playground_parity.mjs.
set -eu

root=$(cd "$(dirname "$0")/../.." && pwd -P)
generator="$root/tools/fe-codec-gen"
project="$root/site/playground"
public="$root/site/src/wasm"

cd "$project"
"$generator" --source web/src/presets.fe --out web/src/serializers.fe
"$generator" --source web/src/presets.fe --eip712 --out web/src/eip712.fe
femp build -t wasm -O s

mkdir -p "$public"
for file in fe_codec_playground.wasm fe_codec_playground.js host.js; do
    cp "build/wasm/web/$file" "$public/$file"
done
echo "copied the module to site/src/wasm/" >&2
