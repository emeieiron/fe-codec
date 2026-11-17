# Writing a format

A format is an ordinary Fe ingot that depends on `fe_codec_core` and implements three
traits: `Encoder`, `Decoder` and `Format`. It needs nothing from the JSON or CBOR ingots and
no change to core. Every type that implements `Serialize` and `Deserialize`, generated or
handwritten, works with the new format straight away.

`examples/compact_format` is a complete example: a positional binary format with no field
keys, in about 500 lines. It is an example, not a supported format.

## The pieces

```fe
pub struct Compact { pub limits: Limits }

impl Format for Compact {
    fn encode<T: Serialize, const N: usize>(self, _ value: T) -> CodecResult<BoundedBuffer<N>> {
        var encoder = CompactEncoder<N>::new(self.limits)
        value.serialize(mut encoder)?
        encoder.finish()
    }

    fn decode<T: Deserialize, const N: usize>(self, _ input: own BoundedBuffer<N>) -> CodecResult<T> {
        var decoder = CompactDecoder<N>::new(input, self.limits)
        let value = T::deserialize(mut decoder)?
        decoder.finish()?
        Result::Ok(value)
    }
}
```

Your encoder and decoder implement the data model:

| Encoder | Decoder | Values |
| --- | --- | --- |
| `write_bool` | `read_bool` | `bool` |
| `write_unsigned`, `write_signed` | `read_unsigned`, `read_signed` | 8–64-bit integers, as `u64`/`i64` plus a width |
| `write_wide_unsigned`, `write_wide_signed` | `read_wide_unsigned`, `read_wide_signed` | 128- and 256-bit integers, as `u256`/`i256` |
| `write_text` | `read_text` | UTF-8 `Text<N>` |
| `write_bytes` | `read_bytes` | Byte strings |
| `write_none`, `write_some` | `read_some` | `Option<T>`: `write_some` marks a value that follows |
| `write_variant` | `read_variant` | Unit enum variants, described by `Schema::variant` |
| `begin_record`, `field`, `end_record` | `begin_record`, `next_field`, `end_record` | Records, described by `Schema::field` |
| `begin_sequence`, `element`, `end_sequence` | `begin_sequence`, `element`, `end_sequence` | Fixed-length arrays |

Choose `OMIT_NONE`: `true` if your format identifies fields by a key and can leave out
`None` fields, `false` if fields are positional.

## Helpers from core

- `ByteWriter<N>` and `ByteReader<N>` write and read bounded buffers.
- `Structure` enforces the traversal rules: one root value, a value after every field
  or element, and the depth and item limits. Call `value` for each scalar, `begin`/`end`
  around containers, `member` for each field or element, and `finish` at the end.
- `Frame` counts a container's entries and, through `key`, checks that record fields
  arrive in strictly ascending ID order. Call it in your encoder's `field` so a serializer
  that breaks the order fails in your format too.
- `checked_unsigned`, `checked_signed` and their wide forms validate a value against its
  width.
- `TextWriter<N>` validates UTF-8 as bytes arrive and reports the offending byte.
- `FieldKey::of<R>(position, offset)` builds what `next_field` returns.

## Rules a format must follow

- Report errors with `CodecError::at(kind, offset)`, where `offset` is the byte where the
  problem was found. Reuse the kinds in `ErrorKind`; their codes are stable.
- Never panic on input. Every malformed input must return an error.
- Give each value one encoding, and reject any other on decode, if your format is meant to
  be deterministic.
- `next_field` must return only positions that exist in `R`'s schema.

## Testing a format

Run your format through the same checks as JSON and CBOR. The conformance function in
`examples/transfer/shared/src/conformance.fe` is generic over `Format`. It covers every
integer width, text, bytes, addresses, options, enums, nested records, arrays, a generic
record, a handwritten serializer and an evolved schema. It returns the number of the first
check that fails, so a failing format points at the problem:

```fe
#[test]
fn compact_passes_conformance() {
    assert!(conformance(Compact::new()) == 0)
}
```

With FeMP, add the format's ingot as a module and run `femp test` and `femp prop` to check
that it behaves the same on the EVM, natively and in WebAssembly.
