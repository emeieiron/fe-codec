# Fe Codec

Fe Codec is a type-safe, extensible serialization framework for Fe. It encodes and decodes
structured data in JSON and deterministic CBOR, and any developer can add a format of their
own. A build-time generator writes serializers for your types from ordinary Fe declarations.
The same types and serializers compile for smart contracts, native programs and
WebAssembly, using fixed-capacity buffers and explicit limits.

| Ingot / tool | Role |
| --- | --- |
| `fe_codec_core` | `Serialize`/`Deserialize`, the `Encoder`/`Decoder` data model, `Format`, schemas, errors, limits, `Text<N>`, byte I/O |
| `fe_codec_json` | Compact JSON |
| `fe_codec_cbor` | Deterministic CBOR (RFC 8949 §4.2.1) |
| `fe_codec_io` | Bounded buffers, re-exported by core |
| `fe-codec-gen` | Writes `Serialize`, `Deserialize` and `Schema` impls for `#[serializable]` types |

The runtime ingots are plain Fe and work with the official `fe` toolchain. FeMP is optional:
it can run the generator for you and test across targets, but nothing you ship depends on it.

## Quick start

Add the ingots to your `fe.toml`, pinned to a release revision. One Git source can supply
several ingots by name.

```toml
[dependencies]
fe_codec_core = { source = "https://example.invalid/fe-codec.git", rev = "<commit>", name = "fe_codec_core" }
fe_codec_json = { source = "https://example.invalid/fe-codec.git", rev = "<commit>", name = "fe_codec_json" }
fe_codec_cbor = { source = "https://example.invalid/fe-codec.git", rev = "<commit>", name = "fe_codec_cbor" }
```

Install the generator from the same revision:

```bash
cargo install --locked --git <repository> --rev <commit> fe-codec-gen
```

Mark your types `#[serializable]`:

```fe
use fe_codec_core::Text
use std::evm::Address

#[serializable]
pub struct Order {
    pub id: u64,
    pub buyer: Address,
    pub total: u256,
    pub status: OrderStatus,
    pub note: Option<Text<32>>,
}

#[serializable]
pub enum OrderStatus {
    Open,
    Paid,
}
```

Generate the serializers into a module beside the models, and commit the result:

```bash
fe-codec-gen --source src/models.fe --out src/serializers.fe
```

Run the same command with `--check` in CI to catch a stale file. Pass `--source` once per
module when your models span several files. The generated module is ordinary Fe that
implements traits on your types; you never call into it directly.

## Examples

These are taken from `examples/vanilla`, whose tests run them with `fe test`.

**1. Encode and decode.** The annotated bindings choose the type and buffer capacity:

```fe
fn round_trip(_ order: Order) -> CodecResult<Order> {
    let bytes: BoundedBuffer<192> = fe_codec_json::encode(order)?
    let back: Order = fe_codec_json::decode(bytes)?
    Result::Ok(back)
}
```

Without annotations, name both generic arguments: `fe_codec_json::encode<Order, 192>(order)`.
Fe does not accept just one of them.

**2. Switch formats without touching the serializer.** Change the ingot, or write code that
is generic over `Format`:

```fe
let cbor = fe_codec_cbor::encode<Order, 64>(order)?

pub fn store<F: Format>(_ format: F, _ order: Order) -> CodecResult<BoundedBuffer<192>> {
    format.encode<Order, 192>(order)
}

store(Json::new(), order)
store(Cbor::with_limits(Limits { max_depth: 1, max_items: 5 }), order)
```

**3. A custom serializer.** Implement the traits on your type to give it its own form.
Here a temperature is written as text such as `"21C"` in every format, and works as a field
of any generated record:

```fe
impl Serialize for Celsius {
    fn serialize<E: Encoder>(self, _ encoder: mut E) -> CodecResult<()> {
        encoder.write_text(self.text()?)
    }
}

impl Deserialize for Celsius {
    fn deserialize<D: Decoder>(_ decoder: mut D) -> CodecResult<Self> {
        let start = decoder.offset()
        match Celsius::parse(decoder.read_text<4>()?) {
            Option::Some(value) => Result::Ok(value),
            Option::None => Result::Err(CodecError::at(kind: ErrorKind::InvalidSyntax, offset: start)),
        }
    }
}
```

Records can be written by hand with the same helpers the generator uses. See
[writing serializers](docs/writing-serializers.md).

**4. A new format.** Implement `Encoder`, `Decoder` and `Format` in your own ingot, against
`fe_codec_core` alone. Every serializable type works with it at once. `examples/compact_format`
is a complete positional binary format, and [writing a format](docs/writing-a-format.md)
explains the contract and how to test against it.

**5. Errors and schema evolution.** Errors carry a kind, the byte offset and the field:

```fe
match fe_codec_json::decode<Order, 25>(input) {
    Result::Err(error) => {
        // error.kind == ErrorKind::UnknownVariant, error.offset == 17,
        // error.field() is the name "status"
    },
    Result::Ok(order) => { ... },
}
```

`error.kind.code()` is a stable number for platform boundaries. On the EVM, `unwrap` on a
failed result reverts with that code and the offset as two ABI words.

Give fields stable wire identities with `#[codec_id]`, and add new fields as `Option`s:

```fe
#[serializable]
pub struct Account {
    #[codec_id = 1]
    pub owner: u64,
    #[codec_id = 2]
    pub balance: u64,
    #[codec_id = 3]
    pub label: Option<Text<8>>,   // added later: old data still decodes
}
```

See [wire profiles](docs/formats.md#schema-evolution) for what each change preserves.

## Supported types

| Type | Notes |
| --- | --- |
| `bool`, `u8`–`u256`, `i8`–`i256` | `usize` and `isize` are rejected: their width differs per target |
| `Text<N>` | UTF-8, up to `N` bytes |
| `BoundedBuffer<N>` | Bytes, up to `N` |
| `core::bytes::PackedBytes<N>`, `std::evm::Address` | Bytes of exactly `N` or 20 |
| `Option<T>` | Anywhere, except `Option<Option<T>>`, which cannot round-trip |
| `[T; N]` | As a field, any length of `Copy` elements, or up to 64 of other elements. Elsewhere, such as inside an `Option` or in handwritten code, decoding needs `T: Copy` and `N > 0` |
| `#[serializable]` structs | Named fields of up to 31-byte names; type and const generic parameters |
| `#[serializable]` enums | Unit variants of up to 31-byte names |
| Your own types | With handwritten impls |

Not supported yet: enums with payloads, variable-length collections, maps, tuples, ignoring
unknown fields, and choosing a serializer per field.

## Limits and buffers

Buffers have a capacity fixed at compile time, and nothing is heap-allocated. Encoding
fails with `CapacityExceeded` at the first byte that does not fit. `Limits` bounds nesting
depth and entries per container. `fe_codec_json::encode` and the other free functions use
`Limits::standard()` (32 levels, 256 entries); `Json::with_limits(..)` and
`Cbor::with_limits(..)` set others.

Move bytes across platform boundaries with `BoundedBuffer<N>::from_span` and `copy_to`.

## Using FeMP (optional)

FeMP can run the generator as part of every build, so generated files never enter your
source tree:

```toml
[modules.shared.generate.serializers]
command = ["fe-codec-gen", "--source", "src/models.fe", "--stdout"]
output = "src/serializers.fe"
```

`--stdout` is the generator's mode for the Fe Source Generator Protocol (FSGP), which FeMP
specifies in its `docs/source-generators.md`. Run `femp generate` once to give your editor
a copy, and ignore that path in Git. FeMP's `femp test`, `femp prop` and `femp prove` then
check your code on the EVM, natively and in WebAssembly.

## Guarantees

Some properties are proved, for every input, on every target. Others are checked with
generated inputs or fixed examples. [Design](docs/design.md#guarantees) lists each one
with its evidence, and what has not been established.

## Developing Fe Codec

```text
core/ io/ formats/       runtime ingots
generator/               fe-codec-gen (Rust)
examples/vanilla/        plain-Fe consumer, used by tests/vanilla.py
examples/transfer/       FeMP project: conformance, properties, proofs, platform demos
examples/compact_format/ third-party format example
tests/                   consumer checks and the reference vector encoder
docs/                    design, wire profiles, extension guides
```

```bash
cargo test --locked
```

```bash
cd examples/transfer && femp test -t evm -t native -t wasm && femp prop -t evm -t native -t wasm && femp prove
```

```bash
python3 tests/vanilla.py --generator target/debug/fe-codec-gen
```

```bash
python3 tests/consumer.py
```

The FeMP project pins Fe 26.6.0. `tests/vanilla.py` checks a plain-Fe copy with
`fe test` on the `sonatina` and `native` backends. `tests/consumer.py` checks a downstream
FeMP project against a temporary Git release, including its native executable and
WebAssembly bindings.
