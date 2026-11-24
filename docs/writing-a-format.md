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
| none | `skip_value` | Discards one value; see below |
| `begin_record`, `field`, `end_record` | `begin_record`, `next_field`, `end_record` | Records, described by `Schema::field` |
| `begin_sequence`, `element`, `end_sequence` | `begin_sequence`, `element`, `end_sequence` | Fixed-length arrays, and tuples, which are sequences of their arity |
| `begin_list` | `begin_list`, `next_element` | `List<T, N>`, a sequence whose length is part of the value; see below |
| `begin_map`, `map_key`, `map_value`, `end_map` | `begin_map`, `next_key`, `map_value`, `end_map` | `Map<K, V, N>`; see below |

Choose `OMIT_NONE`: `true` if your format identifies fields by a key and can leave out
`None` fields, `false` if fields are positional.

`skip_value` consumes exactly one complete value at the current position and returns
nothing. It is optional: the default fails with `Unsupported`, which suits a format that cannot
tell where a value ends, as the compact example cannot. A format that can implements it
and makes `next_field` use it: when the key matches no field and `R::skips_unknown_fields()`
is true, skip the value and move on to the next key; otherwise fail with `UnknownField`.
Skip with the same checks as a value you read: keep `Structure` calls (`value`, or
`begin`, `member` and `end`) and the limits, and check the bytes as strictly, so skipping never
accepts input that reading would reject. Keep key-order and duplicate checks for the
unknown keys too if the format has them.

A list is written with `begin_list(length)`, then `element` for each element and
`end_sequence`. The encoder's default calls `begin_sequence(length)`, which is right when your
format writes a list and an array alike, as JSON and CBOR do. A format whose arrays carry no
length, as the compact example's do, overrides it to write the length first. The decoder reads
one with `begin_list(capacity)`, then `next_element` until it returns false, then
`end_sequence`: `next_element` returns true when another element follows, leaving the decoder
at it. Both decoder methods default to `Unsupported`. A decoder must fail with
`CapacityExceeded` rather than return more than `capacity` elements, at the extra element or,
if it learns the length first, at the start of the list. Check `Limits.max_items` before
the capacity so that every format reports the same error, as the first-party ones do.

A map is written with `begin_map(length)`, then for each entry `map_key`, the key's own
`serialize`, `map_value`, the value's, and `end_map`. The state is the same `Sequence` type
the format uses for sequences. Entries arrive in `MapKey` order, which is the bytewise order
of a deterministic CBOR map, so such a format writes them as they come. The decoder reads one
with `begin_map(capacity)`, then `next_key`, which returns true when another entry follows,
leaving the decoder at its key; the key's own `deserialize`; `map_value`, called once the key
has been read, which is where a format that orders its keys by their bytes checks the order and
repeats of the key it just read (CBOR does, and reports `NonCanonical` or `DuplicateField` at
the key); the value's `deserialize`; and finally `end_map`. A format that does not order its
maps accepts any order: the `Map` deserializer inserts each entry and fails with
`DuplicateField` at a repeated key. A format that writes keys as strings, as JSON does, must
decide what a key may be, and reject the rest. Every map method defaults to `Unsupported`, as
for lists, and so does the compact example's, since its decoder cannot tell a key's order
without its type. As for lists, check `Limits.max_items` before the capacity.

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
- `TextWriter<N>` validates UTF-8 as bytes arrive and reports the offending byte. `Utf8`
  does the same without storing the text, which is what `skip_value` needs.
- `FieldKey::of<R>(position, offset)` builds what `next_field` returns.

A format that reads types rather than values, as EIP-712 does, implements `Describer`. Its
`begin_tuple` and `end_tuple` default to `Unsupported`, so a format with no tuple type needs
no code to reject them, and so do `begin_list` and `end_list` for a format with no dynamic
arrays; EIP-712 writes lists as `T[]`. `begin_map` and `end_map` default the same way, and EIP-712, which has no map type, rejects them naming the field.

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
integer width, text, bytes, addresses, options, enums, nested records, arrays, tuples, a generic
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
