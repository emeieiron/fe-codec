# Design

Fe Codec separates three things, the way kotlinx.serialization does, but builds each from
ordinary Fe traits rather than a compiler plugin:

```
model types ──impl──▶ Serialize / Deserialize / Schema      generated or handwritten
                         │ call
                         ▼
                 Encoder / Decoder  (the data model)        fe_codec_core
                         ▲ impl
     fe_codec_json · fe_codec_cbor · any third-party format, each also a Format
```

- **The value side.** `Serialize` and `Deserialize` are implemented on the value's own type,
  like `core::hash::Hash`. A field is serialized by calling its type's impl, so nested
  records, generic records, types from other modules or ingots, and handwritten
  serializers all compose through trait resolution. `Schema` lists a record's fields
  (name, ID, whether optional) and an enum's variants.
- **The data model.** `Encoder` and `Decoder` are the vocabulary every format speaks:
  booleans, integers up to 256 bits, text, bytes, options, unit variants, records and
  fixed-length sequences, which also carry tuples. Records visit fields in ascending ID
  order; `OMIT_NONE` tells a serializer whether the format leaves out `None` fields.
- **The format side.** A `Format` value (`Json`, `Cbor`, or a third party's) owns its
  limits and drives an encoder or decoder over a bounded buffer. Formats do not depend on
  each other, and the application chooses one where it encodes.

Everything is resolved statically: generic code is monomorphized for each value type and
format, so there is no runtime dispatch, no allocation, and no reflection. The only
runtime metadata is the field and variant names and IDs a format needs.

## What Fe provides and what it does not

These were checked against the Fe 26.6.0 sources and with probe ingots on the EVM and
native backends.

| Fe capability | Used for |
| --- | --- |
| Traits implemented on primitives, `Option<T>`, `[T; N]` and tuples from any ingot that owns the trait | The built-in impls in `core/src/impls.fe` |
| Associated consts, generic and const-generic trait methods | `Encoder::OMIT_NONE`, `Format::encode<T, N>` |
| Inference from an annotated binding through `?` | `let bytes: BoundedBuffer<128> = fe_codec_json::encode(order)?` |
| `Copy` arrays built with `[value; N]` | Decoding `[T; N]` without `unsafe` |
| Unknown attributes are accepted | `#[serializable]`, `#[codec_id]` |

| Constraint | Consequence |
| --- | --- |
| No derive macros, reflection or compile-time code generation | `fe_codec_gen`, a generator written in Fe, writes the impls as ordinary Fe source |
| Partial generic arguments are rejected (`f<A>(x)` for `f<A, const N>`) | Calls name every generic argument, or let both be inferred from an annotated binding |
| `unsafe` in a dependency must be allowed by every consumer | Core has no `unsafe`. Generic `[T; N]` decoding therefore needs `T: Copy`, and cannot produce `[T; 0]`; generated code decodes literal-length arrays element by element instead |
| `usize` is 256, 64 or 32 bits depending on the target | `usize` and `isize` are not serializable |
| `fe test` runs the `sonatina` (EVM) and `native` backends | WebAssembly is checked through FeMP |
| On the EVM, a panic's value becomes revert data | `CodecError` ABI-encodes as its kind code and offset, so `unwrap` compiles in contracts |

## Design decisions

| Decision | Why |
| --- | --- |
| Serializers are trait impls on the value's own type | Calls name only the value type and capacity, and fields compose through trait resolution across files, ingots, generics and handwritten code |
| A schema lists only field and variant names and IDs | That is all a format reads; there is no type graph to keep consistent |
| Every format visits record fields in ascending ID order | One order for all formats. Canonical CBOR needs it, and JSON objects are unordered anyway |
| `OMIT_NONE` is chosen by the format | Keyed formats leave out `None` fields; positional formats cannot |
| Field and variant names are at most 31 bytes | A name fits in one word, which keeps `CodecError` at three words and field lookups to one comparison. This keeps EVM contracts small |
| CBOR fields of 64 bits or fewer accept only plain integers | A canonical bignum never fits them, and keeping the bignum path out of narrow reads saves contract size |
| Models opt in with `#[serializable]` | The selection lives with the model, and a local type used without it gets a generator error |

## Invariants

| | Invariant |
| --- | --- |
| I1 | **Round trip.** `decode(encode(v)) == v` for every supported value and format, within the buffer and limits. |
| I2 | **Canonical output.** Equal values encode to identical bytes. CBOR follows RFC 8949 §4.2.1. |
| I3 | **Strict decoding.** Unknown, duplicate and missing fields, trailing data, non-canonical CBOR, out-of-range integers, invalid UTF-8 and malformed hex are rejected with a typed kind and offset. |
| I4 | **Bounds.** No input or value makes an encoder or decoder panic. Capacity, depth and item limits produce errors. |
| I5 | **Traversal protocol.** One root value; record fields visited in ascending ID order and counted exactly; sequences of exact length. Checked in every first-party format and in the compact example. |
| I6 | **Targets agree.** The EVM, native and WebAssembly builds produce the same bytes and the same errors. |
| I7 | **Evolution.** An appended optional field with a new ID is compatible both ways while `None`; a field keeps its CBOR identity across a rename. |

## Guarantees

Three kinds of evidence back these invariants, and they are not interchangeable.

**Proved** by `femp prove` for every input, separately for the EVM, native and WebAssembly
programs:

| Claim | Where |
| --- | --- |
| A 1-, 2-, 3- or 4-byte sequence completes one UTF-8 character exactly when RFC 3629 §4 allows it, and the validator returns to its start state | `core/src/verification.fe` |
| Bytes that cannot start a character are rejected immediately | `core/src/verification.fe` |
| The CBOR encoder's head for any 64-bit argument is accepted by the decoder, and is the only head of any width the decoder accepts | `formats/cbor/src/verification.fe` |
| The 8–64-bit range checks accept exactly the values of their width | `examples/transfer/shared/src/props.fe` |

The UTF-8 claims cover one character. Text of any length follows by induction, because the
validator resets after each character. That step is an argument, not a machine-checked
proof.

**Tested with generated inputs** by `femp prop` on all three targets (seed `0x2a`; case
counts per run):

| Property | Cases |
| --- | --- |
| Every 8–64-bit integer width round-trips in JSON, CBOR and compact | 500 |
| Every 128- and 256-bit width round-trips | 500 |
| CBOR uses plain integers up to 64 bits and bignums only above | 300 |
| Records with optional text, and byte strings, round-trip | 300 each |
| No input of up to 64 arbitrary bytes makes any decoder panic, for three schemas | 1000 |
| The targets agree on every decode outcome, kind and offset for those inputs | 256 |
| The targets agree on the encoded bytes of wide values | 256 |

`femp prove` reports the round-trip and no-panic properties as not supported: its model does
not yet cover the `mut` borrows the encoder and decoder traits use. They are tested, not proved.

**Example-tested** by `femp test` on all three targets and `fe test` on two:

- golden vectors for every type and both first-party formats, computed by an independent
  reference encoder (`tests/vectors/reference.py`), and RFC 8949 Appendix A vectors for
  integers, bignums, text, byte strings, arrays and maps;
- malformed input, boundary and limit tables with exact error kinds and offsets;
- schema evolution between record versions;
- a 30-check conformance function run against JSON, CBOR and the compact format;
- serializers that break the traversal protocol;
- the generator: every diagnostic with its exact position, the command line, and output that
  compiles (`tests/generator_cases.py`); generated code for random models checked against the
  reference encoders and the EIP-712 specification on two backends, and output unchanged by
  reformatting (`tests/generator_fuzz.py`); its parser against `fe check` on mutated sources
  (`tests/parser_vs_fe.py`).

**Not established:**

- a proof of I1, I3 or I4 for whole encoders and decoders;
- conformance with every JSON parser: values above 2^53 need an integer-preserving parser;
- gas or code-size bounds; the property harness exceeds EIP-170 because it links every
  format, which says nothing about a contract that uses one;
- behavior beyond the Fe 26.6.0 toolchain the tests ran on.

## Source generation

`fe_codec_gen` reads `#[serializable]` declarations with `fe_syntax`, a parser for Fe
declarations that reports syntax errors where `fe check` does, and prints
ordinary Fe: an `impl Schema`, `impl Serialize` and `impl Deserialize` per type, using only
`fe_codec_core`'s public API. Field types are copied as written. The generated module
imports everything its source modules import, and the Fe compiler checks every type. The
generator itself only rejects what the compiler cannot catch:

- `usize`;
- `Option<Option<T>>`, which cannot round-trip;
- a local type that is not `#[serializable]`;
- an array that would need `Copy` elements;
- an empty array inside another type, which core cannot decode;
- invalid IDs and unsupported generic forms.

FeMP runs the generator through the Fe Source Generator Protocol, specified in FeMP's
`docs/source-generators.md`: argv in, Fe source on standard output, diagnostics on standard error. That contract is
already domain-neutral. FeMP knows nothing about serialization, and Fe Codec nothing about
FeMP. Version 1 writes down the existing contract, its determinism and failure rules, and a
`path:line:column: severity: message` diagnostic format whose paths FeMP rewrites to resolve
from the project root. It adds no plugin API, registration or intermediate representation.
Caching was left out on purpose: a command line does not identify the generator binary, and
a stale cache would be a correctness bug.
