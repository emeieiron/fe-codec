# Fe Codec

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

### 1. Install `fe_codec_gen`

Download the prebuilt binary from the [GitHub Releases](https://github.com/emeieiron/fe-codec/releases)
page instead of cloning and compiling the generator yourself (`linux-x86_64`, `linux-arm64`,
`macos-arm64`, or `macos-x86_64`):

```bash
# Replace <target> with linux-x86_64, linux-arm64, macos-arm64, or macos-x86_64
mkdir -p ~/.local/bin
curl -fsSL https://github.com/emeieiron/fe-codec/releases/download/v0.1.0/fe_codec_gen-<target>.tar.gz \
  | tar -xz -C ~/.local/bin
```

Each release includes `SHA256SUMS` to verify downloads. If you are on another platform or an
unreleased commit, you can still build `fe_codec_gen` from a matching checkout with `fe` alone:

```bash
fe build --backend native tools/generator --out-dir ~/.local/bin
```

### Why a separate generator is needed

- **Why code generation is needed:** Every serializable type needs `Serialize`, `Deserialize`,
  `Schema`, and `Describe` trait implementations (and `Eip712Struct` when using `--eip712`).
  Generating these traits lets Fe resolve serialization statically via monomorphization—with no
  runtime reflection, no heap allocation, compact EVM bytecode, and ~13,900 gas for static
  EIP-712 hashing versus ~70,900 gas through the generic path. Writing and maintaining these
  implementations by hand across every struct and enum is repetitive and error-prone.
- **Why it must be a separate tool:** Fe 26.7.0 has no derive/procedural macros, compile-time
  reflection, or compiler plugins. Although the compiler parses and ignores unknown attributes
  such as `#[serializable]` and `#[codec_id]`, it cannot expand them into trait implementations
  during compilation. `fe_codec_gen`—itself written in Fe around the `fe_syntax` declaration
  parser—runs outside the compiler to emit ordinary Fe source beside your models (or to stdout
  via `--stdout` for build systems like FeMP). Shipping prebuilt release binaries keeps consumers
  and CI checks (`fe_codec_gen --check`) from having to clone this repository and build the
  generator from source on every setup.

### 2. Add the ingots to `fe.toml`

Fe pins Git dependencies by full commit hash. Each GitHub Release page lists the exact commit
SHA for that tag:

```toml
[dependencies]
fe_codec_core = { source = "https://github.com/emeieiron/fe-codec.git", rev = "<commit>", name = "fe_codec_core" }
fe_codec_json = { source = "https://github.com/emeieiron/fe-codec.git", rev = "<commit>", name = "fe_codec_json" }
```

### 3. Mark types, generate serializers, and encode/decode

Mark your types `#[serializable]`, generate their serializers beside them (use `--check` in CI
to catch stale output), and encode or decode with bounded buffers:

```fe
#[serializable]
pub struct Order {
    pub id: u64,
    pub buyer: Address,
    pub total: u256,
    pub note: Option<Text<32>>,
}
```

```bash
fe_codec_gen --source src/models.fe --out src/serializers.fe
```

```fe
let bytes: BoundedBuffer<192> = fe_codec_json::encode(order)?
let back: Order = fe_codec_json::decode(bytes)?
```

[Getting started](docs/getting-started.md) continues from here.

## Limitations

Several constraints in the Fe 26.7.0 compiler and toolchain shape Fe Codec's design today:

- **No derive macros, compile-time reflection, or compiler plugins.** Because Fe cannot expand
  `#[serializable]` or `#[eip712]` at compile time, Fe Codec relies on an external CLI
  (`fe_codec_gen`) and a standalone declaration parser (`fe_syntax`) that must track Fe syntax.
  Generated code must live in a sibling `.fe` module, which requires every serialized struct
  field and record-variant field to be `pub`, rules out `where` clauses, generic enums, and
  generic parameter defaults, and requires a second `--eip712` pass to emit compile-time
  `core::keccak` `encodeType` constants.
  *Cleaner solution:* Compiler-integrated `#[derive(Serialize, Deserialize, Eip712)]` (via
  procedural macros or compile-time type reflection with `const fn` evaluation) expanding impls
  directly inside the defining module with access to private fields and full compiler type
  resolution.
- **No safe uninitialized array construction, and `unsafe` propagates to consumers.** Any `unsafe`
  inside a dependency requires downstream consumers to opt in, so `fe_codec_core` contains no
  `unsafe`. Generic `[T; N]` decoding builds `[first; N]` and overwrites elements, which
  requires `T: Copy` and rejects `[T; 0]`; `List<T, N>` and `Map<K, V, N>` likewise initialize
  `[Option::None; N]`, requiring `T: Copy` and `V: Copy`. `fe_codec_gen` works around this on
  direct struct and record-variant fields by unrolling literal-length non-`Copy` arrays up to
  length 64.
  *Cleaner solution:* Safe standard-library array initialization (such as `array::try_from_fn`
  or an encapsulated `MaybeUninit` primitive) or library-encapsulated `unsafe` that does not
  require opt-in from callers.
- **No partial generic arguments.** Fe rejects specifying only one argument for functions that
  take both type and const generic parameters (such as `encode<T, const N>`), forcing callers to
  supply every generic argument (`fe_codec_json::encode<Order, 192>(order)`) or rely on an
  annotated binding (`let bytes: BoundedBuffer<192> = ...`).
  *Cleaner solution:* Partial generic argument lists or inference placeholders (`encode<Order, _>`
  / `encode<_, 192>`).
- **No variadic generics.** `fe_codec_core` hand-writes tuple `Serialize`, `Deserialize`, and
  `Describe` implementations for arities 1–8; larger tuples are unsupported.
  *Cleaner solution:* Variadic generics or compile-time macro expansion in `core`.
- **Full-SHA Git pinning and no toolchain-managed codegen dependencies.** `fe.toml` pins Git
  dependencies by 40-character commit SHA rather than SemVer/tags and cannot declare build or
  codegen tools, so consumers must manually keep `fe_codec_gen` aligned with the `rev` in
  `fe.toml`.
  *Cleaner solution:* Package registry or SemVer/tag resolution in `fe.toml` and
  toolchain-managed macro/codegen dependencies.
- **Generator filesystem and platform limits.** `std::fs` has no atomic rename (so `fe_codec_gen`
  writes in place only after all checks pass), and `fe_codec_gen` currently parses POSIX `/`
  paths only, limiting prebuilt binaries to Linux and macOS (`x86_64` and `arm64`) rather than
  native Windows.
  *Cleaner solution:* Cross-platform path handling and atomic-write primitives in `std::fs` (or
  eliminating the external CLI once compiler-integrated derives exist).

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
The FeMP checks and the generator fuzz test run locally:

```bash
cd examples/transfer && femp test -t evm -t native -t wasm && femp prop -t evm -t native -t wasm && femp prove
```

```bash
python3 tests/consumer.py
```

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
