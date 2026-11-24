# Fe Codec

[![Tests](https://github.com/emeieiron/fe-codec/actions/workflows/tests.yml/badge.svg)](https://github.com/emeieiron/fe-codec/actions/workflows/tests.yml)
[![FeMP checks](https://github.com/emeieiron/fe-codec/actions/workflows/femp.yml/badge.svg)](https://github.com/emeieiron/fe-codec/actions/workflows/femp.yml)

Fe Codec is a type-safe, extensible serialization framework for Fe. Describe a type once: a
generator written in Fe writes its serializers, and the same code encodes it as JSON,
deterministic CBOR or a format you write, and hashes it as EIP-712 typed data, in smart
contracts, native programs and WebAssembly, with fixed-capacity buffers and explicit limits.

**[Documentation](https://emeieiron.github.io/fe-codec/)** ·
**[Playground](https://emeieiron.github.io/fe-codec/playground/)**, the generator running in your browser

| Ingot / tool | Role |
| --- | --- |
| `fe_codec_core` | `Serialize`/`Deserialize`, the `Encoder`/`Decoder` data model, `Format`, schemas, errors, limits, `Text<N>`, `List<T, N>`, `Map<K, V, N>`, byte I/O |
| `fe_codec_json` | Compact JSON |
| `fe_codec_cbor` | Deterministic CBOR (RFC 8949 §4.2.1) |
| `fe_codec_eip712` | EIP-712 struct hashes, signing digests and wallet requests |
| `fe_codec_io` | Bounded buffers, re-exported by core |
| `fe_codec_gen` | Writes `Serialize`, `Deserialize`, `Schema` and `Describe` impls for `#[serializable]` types, and with `--eip712` static EIP-712 hashing for `#[eip712]` structs |

The runtime ingots are plain Fe and work with the official `fe` toolchain, version 26.7.0.
FeMP is optional: it can run the generator for you and test across targets, but nothing you
ship depends on it.

## Getting started

Install `fe_codec_gen` (Linux x86-64/ARM64, macOS Apple silicon):

```sh
curl -fsSL https://github.com/emeieiron/fe-codec/releases/latest/download/installer.sh | sh
```

Or build from source: `fe build --backend native tools/generator --out-dir ~/.local/bin`.

**Why a separate generator?** Serialization uses static traits (`Serialize`, `Deserialize`,
`Schema`, `Describe`, and `Eip712Struct` with `--eip712`) for zero reflection, zero heap
allocation, and ~13,900 EVM gas versus ~70,900 gas generically. Fe 26.7.0 has no derive macros
or compile-time reflection, so `fe_codec_gen` parses `#[serializable]` types outside the
compiler and emits those impls as plain Fe source. Shipping prebuilt binaries keeps consumers
and CI checks (`--check`) from having to clone and build it.

Add the ingots to `fe.toml`, pinned to the release commit hash (listed on each release page):

```toml
[dependencies]
fe_codec_core = { source = "https://github.com/emeieiron/fe-codec.git", rev = "<commit>", name = "fe_codec_core" }
fe_codec_json = { source = "https://github.com/emeieiron/fe-codec.git", rev = "<commit>", name = "fe_codec_json" }
```

Mark your types `#[serializable]`, generate their serializers (`--check` in CI), and
encode/decode:

```fe
#[serializable]
pub struct Order {
    pub id: u64,
    pub buyer: Address,
    pub total: u256,
    pub note: Option<Text<32>>,
}
```

```sh
fe_codec_gen --source src/models.fe --out src/serializers.fe
```

```fe
let bytes: BoundedBuffer<192> = fe_codec_json::encode(order)?
let back: Order = fe_codec_json::decode(bytes)?
```

[Getting started](docs/getting-started.md) continues from here.

## Limitations

| Compiler / toolchain limit | Current workaround | Cleaner solution |
| --- | --- | --- |
| No derive/proc macros or compile-time reflection | External `fe_codec_gen` + `fe_syntax` parser, sibling `.fe` files, `pub` fields required, no `where` or generic enums, separate `--eip712` pass | Compiler-integrated `#[derive(...)]` or compile-time reflection + `const fn` inside the defining module |
| No safe uninitialized array init; `unsafe` propagates to callers | Zero `unsafe` in `core`; `T: Copy` / `V: Copy` on generic `[T; N]`, `List<T, N>`, `Map<K, V, N>`; no generic `[T; 0]`; unrolled field arrays up to 64 | Safe `core` array init (`try_from_fn` / `MaybeUninit`) or library-encapsulated `unsafe` |
| No variadic generics | Hand-written tuple impls capped at 1–8 elements | Variadic generics or macro expansion in `core` |
| Full-SHA Git pinning; no codegen dependencies | Manual 40-char `rev` synced with `fe_codec_gen` | SemVer/registry dependencies and toolchain-managed codegen |
| Native backend host & `std::fs` gaps | Prebuilt binaries only for Linux x86-64/ARM64 and macOS Apple silicon (no Intel macOS or Windows); in-place writes (no atomic rename) | Full host support + atomic rename in `std::fs` (or no external CLI once derives exist) |

See [Design and guarantees](docs/design.md) for the full design rationale and verification
scope.

## Documentation

- [Getting started](docs/getting-started.md)
- [Types and limits](docs/types.md): what can be serialized, buffers, errors and schema evolution
- [The generator](docs/generator.md): every option, its diagnostics, and running it from FeMP
- [EIP-712](docs/eip712.md): hashing and signing typed data
- [Writing serializers](docs/writing-serializers.md) and [writing a format](docs/writing-a-format.md)
- [Wire profiles](docs/formats.md): exactly what each format writes
- [Design and guarantees](docs/design.md): what is proved, what is tested, and what is not established

## Developing Fe Codec

```text
core/ io/ formats/       runtime ingots
tools/                   fe_codec_gen, its core fe_codec_codegen and its parser fe_syntax
examples/vanilla/        plain-Fe consumer, used by tests/vanilla.py
examples/transfer/       FeMP project: conformance, properties, proofs, platform demos
examples/compact_format/ third-party format example
tests/                   generator suites, consumer checks and reference vector encoders
docs/                    the documentation, published by site/
site/                    the documentation site and its playground (Astro)
```

The [tests workflow](.github/workflows/tests.yml) runs the checks that need only `fe`, Python
and Node on every push. [tools/README.md](tools/README.md) describes the generator's suites.
The [FeMP workflow](.github/workflows/femp.yml) runs the FeMP checks on every push to `main`
that touches the code: the tests on every target, the property tests with seed `0x2a`, the
proofs, and a clean consumer project. Each run's summary shows the reports. To run them
locally:

```bash
cd examples/transfer && femp test -t evm -t native -t wasm && femp prop -t evm -t native -t wasm --seed 0x2a && femp prove
```

```bash
python3 tests/consumer.py
```

The generator fuzz test exhausts a hosted runner, so it runs only locally:

```bash
python3 tests/generator_fuzz.py
```

After changing `tools/` or the codec ingots, rebuild the playground's WebAssembly module, which
needs `femp`, and check it:

```bash
site/scripts/build-playground.sh && node tests/playground_parity.mjs
```

## License

[Apache License 2.0](LICENSE).
