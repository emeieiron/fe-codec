# Fe Codec

Fe Codec encodes and decodes Fe values through typed, format-independent serializers. The same serializer can produce a named JSON object or a CBOR map keyed by numeric field ID. The runtime ingots use bounded buffers and ordinary Fe traits on EVM, native, and WebAssembly.

| Component | Responsibility |
| --- | --- |
| `fe_codec_io` | Inline byte buffers, checked readers, and checked writers |
| `fe_codec_core` | Schema descriptors, serializer and format traits, limits, and errors |
| `fe_codec_json` | JSON encoding and decoding for the supported model types |
| `fe_codec_cbor` | Deterministic CBOR encoding and decoding for the same types |
| `fe-codec-gen` | Build-time extraction of Fe declarations and generation of typed serializers |

## Run the FeMP demo

Install FeMP from a checkout with module generation support. With `fe-mp` checked out next to `fe-codec`, run these commands from the Fe Codec root.

~~~sh
cargo install --path ../fe-mp/crates/femp-cli
cd examples/transfer
femp toolchain install
femp doctor
femp check -t evm -t native -t wasm
femp test -t evm -t native -t wasm
femp build -t evm -t native -t wasm
femp package -t evm -t native -t wasm
~~~

The demo pins Fe 26.6.0 and uses Rust 1.97 to build its generator. The `modules.shared.generate.serializers` entry in the demo manifest selects `Batch` and `Sample` from `shared/src/models.fe`. FeMP runs the generator once per command and inserts its output as `src/serializers.fe` in the staged shared ingot. Each backend compiles those same serializers. [Demo manifest](examples/transfer/femp.toml) · [Fe 26.6.0 release](https://github.com/argotorg/fe/releases/tag/v26.6.0)

The tests compare literal JSON and CBOR bytes and decode the same fixtures on all three targets. The example exports a two-format verification through an EVM contract, a native executable, and a WASM function. `femp package` collects the artifacts and bindings under `examples/transfer/dist/`.

## Use Fe Codec in another project

Declare the runtime ingots in your application's `fe.toml`. A pinned Git revision can
supply multiple ingots by name, including their transitive dependencies.

~~~toml
[dependencies]
fe_codec_core = { source = "https://example.invalid/fe-codec.git", rev = "<commit>", name = "fe_codec_core" }
fe_codec_json = { source = "https://example.invalid/fe-codec.git", rev = "<commit>", name = "fe_codec_json" }
~~~

Use your release repository and commit in both entries. Install the matching
`fe-codec-gen` from that revision with `cargo install --locked --git <repository> --rev <commit> fe-codec-gen`.
FeMP resolves the codec ingots from the Fe dependency graph, including for WASM.

~~~toml
[modules.shared.generate.serializers]
command = ["fe-codec-gen", "--source", "src/models.fe", "--type", "Transfer", "--stdout"]
output = "src/serializers.fe"
~~~

FeMP runs generation during its normal commands. Run `femp generate` once to
make the configured modules visible to the Fe language server in the source tree,
and add those output paths to your ignore list. Later FeMP commands refresh the
editor copies when they run the corresponding generators.

Run `python3 tests/consumer.py` with both binaries on PATH to verify a clean
consumer project against a temporary Git release. Pass `--source` and `--rev`
to verify a published revision.

## Model support

The generator supports booleans, `u8` through `u64`, `i8` through `i64`, `Text<N>`, named records, unit enums, fixed arrays with literal lengths from 0 to 64, and outer `Option<T>` fields. Use `Text` from `fe_codec_core` and the prelude's `Option` or `core::Option`. Names, including qualified schema names, can occupy up to 64 UTF-8 bytes. Other attributes, arbitrary aliases, generic models, enum payloads, and recursive models are outside the v0.1 generator's input grammar.

## Encode and decode

Add `fe_codec_core` and the selected format ingots as path dependencies in the application ingot. The runtime ingots are ordinary Fe libraries. [Demo ingot](examples/transfer/shared/fe.toml)

Import the model and its generated serializer. Buffer capacity is a compile-time argument, and `Limits` controls nesting depth and container size. `Limits::standard()` permits 32 nested containers and 256 entries per container.

~~~fe
use fe_codec_core::{BoundedBuffer, CodecResult, Limits}
use ingot::models::Transfer
use ingot::serializers::TransferSerializer

pub fn encode_transfer(_ value: Transfer) -> CodecResult<BoundedBuffer<128>> {
    fe_codec_json::encode<Transfer, TransferSerializer, 128>(value, Limits::standard())
}
~~~

Use the same type and serializer with `fe_codec_cbor::encode`, or pass a populated `BoundedBuffer<N>` to either format's `decode<T, S, N>`. `Text<N>::from_bytes` validates UTF-8 over the buffer's initialized prefix. Bounds and decoding failures return `CodecError` with a typed `kind` and a zero-based byte `offset`. An offset can equal the input length when data is missing. Output capacity errors identify the first byte that did not fit. For known duplicate and missing fields, `error.field()` supplies the schema node and declaration index. Pass those to the serializer's `field` descriptor lookup to get the field name.

Use `BoundedBuffer<N>::from_span` to import a platform buffer and `copy_to` to export the initialized bytes into a caller-owned span. The native demo supports
`codec_transfer_native '{"value":42}'` and `codec_transfer_native --cbor '{"value":42}'`.
An input such as `{}` reports the missing `value` field. The WASM binding
accepts a JSON string through `decodeJsonValue` and returns a tagged value or codec error.

A handwritten serializer implements `SerialDescriptor` and `Serializer<T>`. Descriptor node indices refer to a schema-local graph. A record exposes indexed field descriptors and a sequence exposes its element node. Encoding and decoding must each traverse one complete root. Open a record or sequence, select a field or element before visiting its value, and close the container after traversal. For record decode, track field presence to reject duplicate assignments and missing required fields. The scalar serializer in the conformance suite shows the smallest complete implementation. [Conformance suite](examples/transfer/shared/src/tests.fe)

A format implements `Encoder` and `Decoder` and chooses how to represent the schema and container state. It can use the checked byte I/O and the core `Structure` helper for limits and traversal validation. Dependencies flow from the format to core and its byte I/O. Format selection stays at the application call site.

## Wire profiles and compatibility

Both profiles support the model types listed above. They omit `Option::None` fields when encoding and accept absence or explicit `null` when decoding. They validate UTF-8, require exact fixed-array lengths, and reject unknown fields, duplicate fields, missing required fields, and trailing data. Text length is measured in UTF-8 bytes, including any NUL bytes.

- **JSON** emits compact UTF-8 with fields in declaration order, enum variant names as strings, and integers as decimal JSON numbers. It accepts reordered members and Unicode escapes. Integer fields reject fractional and exponent notation. Consumers reading integers outside the safe range of an IEEE 754 binary64 number need an integer-preserving JSON decoder. [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259.html)
- **CBOR** emits definite-length maps and arrays. Record keys are positive unsigned integers in ascending order. Generated field IDs are one-based declaration positions, and unit enum values are zero-based declaration ordinals. Decoding enforces shortest encodings and key order. The supported profile has no floating-point values, bignum tags, or indefinite-length containers. [RFC 8949 §4.2.1](https://www.rfc-editor.org/rfc/rfc8949.html#section-4.2.1)

Use `#[codec_id = 9]` on each field or enum variant of a schema that must retain
wire identity across declaration changes. IDs must be unique within a type and
field IDs must be positive. JSON keeps declaration order and names. CBOR writes
numeric field IDs in ascending order. Types that omit explicit IDs use declaration
positions, so reordering is wire-breaking for those types. Appending an optional
field allows newer readers to decode older records, while older readers reject
that field when it appears on the wire.
