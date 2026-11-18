#!/usr/bin/env node
// Checks that the playground's WebAssembly generator, as committed in site/src/wasm/,
// behaves exactly like the native fe_codec_gen: every single-source case in
// tests/generator_corpus, in each mode the case lists, must give the same module or the same
// error line. A stale module (one not rebuilt after a change to tools/) fails here.
//
//     node tests/playground_parity.mjs
//
// It needs fe on PATH for tools/fe-codec-gen, and Node 22 or later.

import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const corpus = join(root, "tests/generator_corpus");
const generator = join(root, "tools/fe-codec-gen");
const { FeCodecPlayground } = await import(join(root, "site/src/wasm/fe_codec_playground.js"));

let words = [];
const wasm = await FeCodecPlayground.load(readFileSync(join(root, "site/src/wasm/fe_codec_playground.wasm")), {
  onLog: (value) => words.push(BigInt(value)),
});

/** The pieces of text the module sent since the last call. */
function pieces() {
  const out = [];
  let i = 0;
  while (i < words.length) {
    const length = Number(words[i++]);
    const bytes = new Uint8Array(length);
    for (let b = 0; b < length; b++) bytes[b] = Number((words[i + (b >> 3)] >> BigInt(8 * (b & 7))) & 0xffn);
    i += Math.ceil(length / 8);
    out.push(new TextDecoder().decode(bytes));
  }
  words = [];
  return out;
}

/** The generator's sources and modes from a case.toml, read with a few patterns. */
function readCase(dir) {
  const toml = readFileSync(join(dir, "case.toml"), "utf8");
  const list = (key) => {
    const match = toml.match(new RegExp(`^${key}\\s*=\\s*\\[([^\\]]*)\\]`, "m"));
    return match ? [...match[1].matchAll(/"([^"]*)"/g)].map((m) => m[1]) : undefined;
  };
  return { sources: list("sources") ?? [], modes: list("modes") ?? ["serializers", "eip712"] };
}

function native(dir, source, eip712) {
  const work = mkdtempSync(join(tmpdir(), "playground-parity-"));
  try {
    mkdirSync(join(work, "src"));
    writeFileSync(join(work, "fe.toml"), '[ingot]\nname = "test_models"\n');
    writeFileSync(join(work, "src/models.fe"), readFileSync(join(dir, source)));
    const args = ["--source", "src/models.fe", "--stdout", ...(eip712 ? ["--eip712"] : [])];
    try {
      return { status: 0, text: execFileSync(generator, args, { cwd: work, stdio: ["ignore", "pipe", "pipe"] }).toString() };
    } catch (error) {
      return { status: error.status, text: error.stderr.toString().replace(/\n$/, "") };
    }
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
}

let passed = 0;
const failures = [];
for (const name of readdirSync(corpus).sort()) {
  const dir = join(corpus, name);
  if (!existsSync(join(dir, "case.toml"))) continue;
  const { sources, modes } = readCase(dir);
  if (sources.length !== 1) continue;
  for (const mode of modes) {
    const eip712 = mode === "eip712";
    const expected = native(dir, sources[0], eip712);
    const reply = wasm.generateModule(readFileSync(join(dir, sources[0]), "utf8"), eip712);
    const [text = ""] = pieces();
    const status = reply.ok ? reply.value : -1;
    if (status === expected.status && text === expected.text) {
      passed += 1;
    } else {
      failures.push(`${name} [${mode}]: native ${expected.status} ${JSON.stringify(expected.text.slice(0, 120))}, wasm ${status} ${JSON.stringify(text.slice(0, 120))}`);
    }
  }
}

for (const failure of failures) console.log(`FAIL ${failure}`);
console.log(`playground parity: ${passed} passed, ${failures.length} failed`);
process.exit(failures.length === 0 ? 0 : 1);
