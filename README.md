# Fe Codec

Fe Codec is a type-safe, extensible serialization framework for Fe. Describe a type once: a
generator written in Fe writes its serializers, and the same code encodes it as JSON,
deterministic CBOR or a format you write, and hashes it as EIP-712 typed data, in smart
contracts, native programs and WebAssembly, with fixed-capacity buffers and explicit limits.

**[Documentation](https://emeieiron.github.io/fe-codec/)** ·
**[Playground](https://emeieiron.github.io/fe-codec/playground/)**, the generator running in your browser

| Ingot / tool | Role |
| --- | --- |
| `fe_codec_core` | `Serialize`/`Deserialize`, the `Encoder`/`Decoder` data model, `Format`, schemas, errors, limits, `Text<N>`, byte I/O |
| `fe_codec_json` | Compact JSON |
| `fe_codec_cbor` | Deterministic CBOR (RFC 8949 §4.2.1) |
| `fe_codec_eip712` | EIP-712 struct hashes, signing digests and wallet requests |
| `fe_codec_io` | Bounded buffers, re-exported by core |
| `fe_codec_gen` | Writes `Serialize`, `Deserialize`, `Schema` and `Describe` impls for `#[serializable]` types, and with `--eip712` static EIP-712 hashing for `#[eip712]` structs |

The runtime ingots are plain Fe and work with the official `fe` toolchain, version 26.6.0.
FeMP is optional: it can run the generator for you and test across targets, but nothing you
ship depends on it.

## Quick start

Add the ingots to your `fe.toml`. Fe pins a Git dependency by its full commit hash; use the
commit of a release tag, such as `v0.1.0`:

```toml
[dependencies]
fe_codec_core = { source = "https://github.com/emeieiron/fe-codec.git", rev = "<commit>", name = "fe_codec_core" }
fe_codec_json = { source = "https://github.com/emeieiron/fe-codec.git", rev = "<commit>", name = "fe_codec_json" }
```

Build the generator from the same commit, with the Fe toolchain alone:

```bash
fe build --backend native tools/generator --out-dir ~/.local/bin
```

Mark your types `#[serializable]`, and generate their serializers beside them:

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
The FeMP checks run locally:

```bash
cd examples/transfer && femp test -t evm -t native -t wasm && femp prop -t evm -t native -t wasm && femp prove
```

```bash
python3 tests/consumer.py
```

After changing `tools/` or the codec ingots, rebuild the playground's WebAssembly module, which
needs `femp`, and check it:

```bash
site/scripts/build-playground.sh && node tests/playground_parity.mjs
```

## License

[Apache License 2.0](LICENSE).
